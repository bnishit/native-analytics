'use strict';
const setup = document.getElementById('tracker-setup');
const task = document.getElementById('agent-task');
function updateAgentSetup() {
  const scope = task?.value === 'audit:read' ? 'audit:read' : 'analytics:read';
  if (setup) setup.textContent = 'codex mcp add afterspan --url ' + location.origin + '/mcp\ncodex mcp login afterspan --scopes ' + scope;
}
if (task) task.onchange = updateAgentSetup;
updateAgentSetup();
