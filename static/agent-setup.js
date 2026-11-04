'use strict';
const setup = document.getElementById('tracker-setup');
if (setup) setup.textContent = 'codex mcp add afterspan --url ' + location.origin + '/mcp\ncodex mcp login afterspan --scopes audit:read';
