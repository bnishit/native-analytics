"""Build artifacts, install outside the checkout, and ingest their real payloads locally."""
import argparse
from email.parser import BytesParser
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import venv
import zipfile

REPO = Path(__file__).resolve().parents[1]
MODULES = ['native_analytics', 'afterspan_tracing', 'afterspan_model', 'afterspan_experiments', 'afterspan_langchain']
NODE_MODULES = ['index', 'native-analytics', 'tracing-node', 'model-observation']


def run(command, cwd, *, env=None, succeeds=True):
    process = subprocess.run(list(map(str, command)), cwd=cwd, env=env, capture_output=True, text=True)
    if succeeds and process.returncode or not succeeds and process.returncode == 0:
        raise RuntimeError(f'Unexpected exit {process.returncode}: {command}\n{process.stdout}\n{process.stderr}')
    return process


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--service-python', default=sys.executable)
    parser.add_argument('--node', default='node')
    args = parser.parse_args()
    node = shutil.which(args.node) or args.node
    npm = Path(shutil.which('npm')).resolve()
    with tempfile.TemporaryDirectory(prefix='afterspan-installed-sdk-') as directory:
        root = Path(directory)
        user_config, global_config = root / 'empty-user-config', root / 'empty-global-config'
        user_config.write_text(''); global_config.write_text('')
        clean = {'PATH': os.environ['PATH'], 'TMPDIR': str(root), 'PIP_CONFIG_FILE': os.devnull,
                 'npm_config_userconfig': str(user_config), 'npm_config_globalconfig': str(global_config),
                 'npm_config_cache': str(root / 'npm-cache')}
        version = run([node, '--version'], root, env=clean).stdout.strip()
        assert int(version.split('.')[0][1:]) >= 24
        run([sys.executable, '-m', 'build', '--wheel', '--no-isolation', REPO / 'sdk/python', '--outdir', root], root)
        assert (REPO / 'sdk/python/LICENSE').read_bytes() == (REPO / 'LICENSE').read_bytes(), 'Python license drift'
        wheel = next(root.glob('*.whl'))
        with zipfile.ZipFile(wheel) as archive:
            names = set(archive.namelist())
            expected = {name + '.py' for name in MODULES}
            metadata = {f'afterspan_sdk-0.1.0.dist-info/{name}' for name in ['METADATA', 'WHEEL', 'RECORD', 'top_level.txt', 'licenses/LICENSE']}
            assert names == expected | metadata, names
            for name in MODULES:
                assert archive.read(name + '.py') == (REPO / f'sdk/python/{name}.py').read_bytes()
            assert archive.read('afterspan_sdk-0.1.0.dist-info/licenses/LICENSE') == (REPO / 'LICENSE').read_bytes()
            package_metadata=BytesParser().parsebytes(archive.read('afterspan_sdk-0.1.0.dist-info/METADATA'))
            assert package_metadata.get_all('Requires-Dist')==['langchain-core<2,>=1.7.4; extra == "langchain"']
        packed = run([node, npm, 'pack', '--json', '--pack-destination', root], REPO, env=clean)
        tarball = root / json.loads(packed.stdout)[0]['filename']
        with tarfile.open(tarball) as archive:
            names = {member.name for member in archive if member.isfile()}
            expected = {'package/' + name for name in ['package.json', 'LICENSE', 'README.md']}
            expected |= {f'package/dist/node/{module}.{extension}' for module in NODE_MODULES for extension in ['js', 'd.ts']}
            assert names == expected, names
            assert archive.extractfile('package/LICENSE').read() == (REPO / 'LICENSE').read_bytes()
            config = json.load(archive.extractfile('package/package.json'))
            assert not config.get('dependencies') and config['private'] is True
            for name in names:
                if name.endswith(('.js', '.d.ts')):
                    assert b".ts'" not in archive.extractfile(name).read(), name
        python_home = root / 'python-consumer'
        venv.EnvBuilder(with_pip=True).create(python_home)
        python = python_home / 'bin/python'
        run([python, '-m', 'pip', 'install', '--no-index', '--no-deps', wheel], root, env=clean)
        run([python, REPO / 'sdk/packaging/consumer.py', root], root, env=clean)
        absent=run([python,'-c','import afterspan_langchain'],root,env=clean,succeeds=False)
        assert 'langchain_core' in absent.stderr
        run([python,'-m','pip','install','langchain-core==1.7.4','anthropic==2.4.0'],root,env=clean)
        run([python,REPO/'sdk/packaging/framework_consumer.py',REPO,root],root,env=clean)
        # Imports must work without the checkout on sys.path or PYTHONPATH.
        assert 'PYTHONPATH' not in clean
        node_home = root / 'node-consumer'
        node_home.mkdir()
        (node_home / 'package.json').write_text('{"private":true,"type":"module"}')
        run([node, npm, 'install', '--offline', '--no-audit', '--no-fund', tarball], node_home, env=clean)
        for fixture in ['consumer.mjs', 'consumer.ts']:
            shutil.copyfile(REPO / 'sdk/packaging' / fixture, node_home / fixture)
        run([node, node_home / 'consumer.mjs', root / 'node-events.json'], node_home, env=clean)
        run([node, REPO / 'node_modules/typescript/bin/tsc', '--noEmit', '--strict', '--target', 'ES2022', '--module', 'NodeNext', '--moduleResolution', 'NodeNext', 'consumer.ts'], node_home, env=clean)
        # A genuinely missing companion in the installed artifacts must fail imports.
        (python_home / 'lib' / f'python{sys.version_info.major}.{sys.version_info.minor}' / 'site-packages/afterspan_model.py').unlink()
        bad_python = run([python, '-c', 'import afterspan_tracing'], root, env=clean, succeeds=False)
        assert 'afterspan_model' in bad_python.stderr
        (node_home / 'node_modules/@afterspan/sdk/dist/node/model-observation.js').unlink()
        bad_node = run([node, '--input-type=module', '-e', "import '@afterspan/sdk'"], node_home, env=clean, succeeds=False)
        assert 'ERR_MODULE_NOT_FOUND' in bad_node.stderr
        # Restore the same intact package artifacts and rerun the real consumers green.
        run([python, '-m', 'pip', 'install', '--force-reinstall', '--no-index', '--no-deps', wheel], root, env=clean)
        shutil.rmtree(node_home / 'node_modules')
        run([node, npm, 'install', '--offline', '--no-audit', '--no-fund', tarball], node_home, env=clean)
        run([python, REPO / 'sdk/packaging/consumer.py', root / 'restored'], root, env=clean)
        run([node, node_home / 'consumer.mjs', root / 'node-events-restored.json'], node_home, env=clean)
        ingestion = run([args.service_python, REPO / 'sdk/packaging/ingest.py', REPO, root], root, env=clean)
        artifacts = REPO / 'data/sdk-packages'
        artifacts.mkdir(parents=True, exist_ok=True)
        for artifact in [wheel, tarball]:
            shutil.copyfile(artifact, artifacts / artifact.name)
        report = {'artifacts': [str(artifacts / wheel.name), str(artifacts / tarball.name)], 'node': version, 'python': sys.version.split()[0], 'wheel_sha256': hashlib.sha256(wheel.read_bytes()).hexdigest(), 'tarball_sha256': hashlib.sha256(tarball.read_bytes()).hexdigest(), 'negative_controls': {'missing_python_companion': bad_python.returncode, 'missing_node_companion': bad_node.returncode}, 'fresh_install_and_restore': 'passed', 'service_ingestion': json.loads(ingestion.stdout)}
        (REPO / 'data').mkdir(exist_ok=True)
        (REPO / 'data/sdk-package-check.json').write_text(json.dumps(report, indent=2))
        print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
