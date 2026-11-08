"""Keep SFTP-owned configs readable by the dedicated Xray service after upload."""
import grp
import os
import pwd
import stat

assert os.geteuid()==0
owner=pwd.getpwnam('ubuntu').pw_uid
group=grp.getgrnam('router-xray').gr_gid
for path in ('/etc/xray/config.json','/etc/xray/nodes.json'):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid not in (0,owner):
            raise RuntimeError('Expected a regular root/ubuntu-owned configuration file')
        os.fchown(fd,owner,group)
        os.fchmod(fd,0o640)
    finally:os.close(fd)
