import sys
import os

sys.path.insert(0, r'C:\kamil')
os.chdir(r'C:\kamil')

import uvicorn

if __name__ == '__main__':
    from interfaces.web.app import app
    # Bind to loopback ONLY. host='0.0.0.0' exposed Kamil's agent/tool endpoints to the whole
    # local network (any device could drive /execute_tool, shell, files). The Electron app and
    # everything else talk to 127.0.0.1:3000, so this changes nothing for legitimate use.
    uvicorn.run(app, host='127.0.0.1', port=3000, reload=False)