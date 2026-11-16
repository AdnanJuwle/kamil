import sys
import os

# Run from Kamil's own tree wherever it is checked out -- no drive letters.
_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _ROOT)
os.chdir(_ROOT)

import uvicorn

if __name__ == '__main__':
    from interfaces.web.app import app
    # Bind to loopback ONLY. host='0.0.0.0' exposed Kamil's agent/tool endpoints to the whole
    # local network (any device could drive /execute_tool, shell, files). The Electron app and
    # everything else talk to 127.0.0.1:3000, so this changes nothing for legitimate use.
    host = os.getenv('KAMIL_BIND_HOST', '127.0.0.1')
    port = int(os.getenv('KAMIL_PORT', '3000'))
    uvicorn.run(app, host=host, port=port, reload=False)