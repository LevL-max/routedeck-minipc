import sys,json
from pathlib import Path
sys.path.insert(0,'/usr/local/lib/router-updater')
from transport import Transport as BaseTransport
from router_platform import wan_interface
from router_platform import wan_interface
class Transport(BaseTransport):
    def __init__(self,route='current'):
        super().__init__(route,wan=wan_interface())
