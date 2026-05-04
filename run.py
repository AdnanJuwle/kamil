import sys
import os

sys.path.insert(0, r'C:\kamil')
os.chdir(r'C:\kamil')

import uvicorn

if __name__ == '__main__':
    from interfaces.web.app import app
    uvicorn.run(app, host='0.0.0.0', port=3000, reload=False)