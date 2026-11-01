import os

import uvicorn
from dotenv import load_dotenv

load_dotenv()

if __name__ == '__main__':
    uvicorn.run('panel.app:app', host=os.getenv('PANEL_HOST', '127.0.0.1'), port=int(os.getenv('PANEL_PORT', '8090')),
                workers=1, proxy_headers=False)
