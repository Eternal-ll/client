
from git import Version

__author__ = 'Sheeo'

FEATURED_MODS = [
    "faf",
    "coop",
    "gw",
    "balancetesting",
    "ladder1v1",
    "matchmaker",
    "nomads",
    "murderparty",
    "labwars",
    "wyvern",
    "blackops",
    "xtremewars",
    "diamond",
    "phantomx",
    "vanilla",
    "civilians",
    "koth",
    "claustrophobia",
    "supremeDestruction"
]


class ModError(Exception):
    def __init__(self, msg):
        super(ModError, self).__init__(msg)


class Mod():
    """
    Represents a mod loadable by FA
    """
    def __init__(self, name, path, version):
        if not isinstance(version, Version):
            raise ModError("Not given a version "+repr(version))
        self._name = name
        self._path = path
        self._version = version

    @property
    def is_featured(self):
        return self._name in FEATURED_MODS

    @property
    def version(self):
        return self._version

    @property
    def name(self):
        return self._name

    @property
    def path(self):
        return self._path

    @property
    def is_compressed(self):
        return False

    @property
    def mount_point(self):
        if self.is_featured:
            return '/'
        else:
            return '/mods/'

    def to_dict(self):
        return {'name':self.name,'path':self.path,'version:':self.version.to_dict()}