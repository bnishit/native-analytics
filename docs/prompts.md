# Prompt library

The **Prompt library** is for prompts an editor chooses to save. Each prompt has an immutable, numbered version. New versions require the latest version number, so stale edits cannot replace newer text. The saved text is encrypted with AES-GCM and the deployment's `AFTERSPAN_CONNECTION_KEY`. Prompt names are plain metadata and are not encrypted.

The library never imports prompt text from a model trace, and Afterspan never sends the text to a model or provider. Open and copy a version into the application that uses it. This is prompt storage and version history, not a prompt execution or experiment runner.

Prompt reads and writes have separate remote OAuth scopes: `prompts:read` and `prompts:write`. Both also require editor membership in the selected project. Keep the encryption key backed up and stable. If it is lost or changed without migrating ciphertext, saved prompts cannot be decrypted. Deleting a prompt removes its encrypted versions.

Limits: 100 active prompts per project, 100 versions per prompt, and 20 KB of UTF-8 text per version. Prompt text does not enter normal trace metadata or event search.
