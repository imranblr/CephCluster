import os
try:
    from urllib.parse import urlparse
except ImportError:
    from urlparse import urlparse

from ceph_deploy.lib import remoto
from ceph_deploy.util import templates


class PackageManager(object):
    """
    Base class for all Package Managers
    """

    def __init__(self, remote_conn):
        self.remote_info = remote_conn
        self.remote_conn = remote_conn.conn

    def _run(self, cmd, **kw):
        return remoto.process.run(
            self.remote_conn,
            cmd,
            **kw
        )

    def _check(self, cmd, **kw):
        return remoto.process.check(
            self.remote_conn,
            cmd,
            **kw
        )

    def install(self, packages, **kw):
        """Install packages on remote node"""
        raise NotImplementedError()

    def remove(self, packages, **kw):
        """Uninstall packages on remote node"""
        raise NotImplementedError()

    def clean(self):
        """Clean metadata/cache"""
        raise NotImplementedError()

    def add_repo_gpg_key(self, url):
        """Add given GPG key for repo verification"""
        raise NotImplementedError()

    def add_repo(self, name, url, **kw):
        """Add/rewrite a repo file"""
        raise NotImplementedError()

    def remove_repo(self, name):
        """Remove a repo definition"""
        raise NotImplementedError()


class RPMManagerBase(PackageManager):
    """
    Base class to hold common pieces of Yum and DNF
    """

    executable = None
    name = None

    def install(self, packages, **kw):
        if isinstance(packages, str):
            packages = [packages]

        extra_flags = kw.pop('extra_install_flags', None)
        cmd = [
            self.executable,
            '-y',
            'install',
        ]
        if extra_flags:
            if isinstance(extra_flags, str):
                extra_flags = [extra_flags]
            cmd.extend(extra_flags)

        cmd.extend(packages)
        return self._run(cmd)

    def remove(self, packages, **kw):
        if isinstance(packages, str):
            packages = [packages]

        extra_flags = kw.pop('extra_remove_flags', None)
        cmd = [
            self.executable,
            '-y',
            '-q',
            'remove',
        ]
        if extra_flags:
            if isinstance(extra_flags, str):
                extra_flags = [extra_flags]
            cmd.extend(extra_flags)
        cmd.extend(packages)
        return self._run(cmd)

    def clean(self, item=None):
        item = item or 'all'
        cmd = [
            self.executable,
            'clean',
            item,
        ]

        return self._run(cmd)

    def add_repo_gpg_key(self, url):
        cmd = ['rpm', '--import', url]
        self._run(cmd)

    def add_repo(self, name, url, **kw):
        gpg_url = kw.pop('gpg_url', None)
        if gpg_url:
            self.add_repo_gpg_key(gpg_url)
            gpgcheck=1
        else:
            gpgcheck=0

        # RPM repo defaults
        description = kw.pop('description', '%s repo' % name)
        enabled = kw.pop('enabled', 1)
        proxy = kw.pop('proxy', '') # will get ignored if empty
        _type = 'repo-md'
        baseurl = url.strip('/')  # Remove trailing slashes

        ceph_repo_content = templates.custom_repo(
            reponame=name,
            name=description,
            baseurl=baseurl,
            enabled=enabled,
            gpgcheck=gpgcheck,
            _type=_type,
            gpgkey=gpg_url,
            proxy=proxy,
            **kw
        )

        self.remote_conn.remote_module.write_yum_repo(
            ceph_repo_content,
            '%s.repo' % name
        )

    def remove_repo(self, name):
        filename = os.path.join(
            '/etc/yum.repos.d',
            '%s.repo' % name
        )
        self.remote_conn.remote_module.unlink(filename)


class DNF(RPMManagerBase):
    """
    The DNF Package manager
    """

    executable = 'dnf'
    name = 'dnf'

    def install(self, packages, **kw):
        extra_install_flags = kw.pop('extra_install_flags', [])
        if '--best' not in extra_install_flags:
            extra_install_flags.append('--best')
        super(DNF, self).install(
            packages,
            extra_install_flags=extra_install_flags,
            **kw
        )


class Yum(RPMManagerBase):
    """
    The Yum Package manager
    """

    executable = 'yum'
    name = 'yum'


class Apt(PackageManager):
    """
    Apt package management
    """

    executable = [
        'env',
        'DEBIAN_FRONTEND=noninteractive',
        'DEBIAN_PRIORITY=critical',
        'apt-get',
        '--assume-yes',
        '-q',
    ]
    name = 'apt'

    def install(self, packages, **kw):
        if isinstance(packages, str):
            packages = [packages]

        extra_flags = kw.pop('extra_install_flags', None)
        cmd = self.executable + [
            '--no-install-recommends',
            'install'
        ]

        if extra_flags:
            if isinstance(extra_flags, str):
                extra_flags = [extra_flags]
            cmd.extend(extra_flags)
        cmd.extend(packages)
        return self._run(cmd)

    def remove(self, packages, **kw):
        if isinstance(packages, str):
            packages = [packages]

        extra_flags = kw.pop('extra_remove_flags', None)
        cmd = self.executable + [
            '-f',
            '--force-yes',
            'remove'
        ]
        if extra_flags:
            if isinstance(extra_flags, str):
                extra_flags = [extra_flags]
            cmd.extend(extra_flags)

        cmd.extend(packages)
        return self._run(cmd)

    def clean(self):
        cmd = self.executable + ['update']
        return self._run(cmd)

    def add_repo_gpg_key(self, url):
        return
        gpg_path = url.split('file://')[-1]
        if not url.startswith('file://'):
            cmd = ['wget', '-O', 'release.asc', url ]
            self._run(cmd, stop_on_nonzero=False)
        gpg_file = 'release.asc' if not url.startswith('file://') else gpg_path
        cmd = ['apt-key', 'add', gpg_file]
        self._run(cmd)

    def add_repo(self, name, url, **kw):
        gpg_url = kw.pop('gpg_url', None)
        if gpg_url:
            self.add_repo_gpg_key(gpg_url)

        safe_filename = '%s.list' % name.replace(' ', '-')
        mode = 0o644
        if urlparse(url).password:
            mode = 0o600
            self.remote_conn.logger.info(
                "Creating repo file with mode 0600 due to presence of password"
            )
        self.remote_conn.remote_module.write_sources_list(
            url,
            self.remote_info.codename,
            safe_filename,
            mode
        )

        # Add package pinning for this repo
        fqdn = urlparse(url).hostname
        self.remote_conn.remote_module.set_apt_priority(fqdn)

    def remove_repo(self, name):
        safe_filename = '%s.list' % name.replace(' ', '-')
        filename = os.path.join(
            '/etc/apt/sources.list.d',
            safe_filename
        )
        self.remote_conn.remote_module.unlink(filename)


class Zypper(PackageManager):
    """
    Zypper package management
    """

    executable = [
        'zypper',
        '--non-interactive',
        '--quiet'
    ]
    name = 'zypper'

    def install(self, packages, **kw):
        if isinstance(packages, str):
            packages = [packages]

        extra_flags = kw.pop('extra_install_flags', None)
        cmd = self.executable + ['install']
        if extra_flags:
            if isinstance(extra_flags, str):
                extra_flags = [extra_flags]
            cmd.extend(extra_flags)
        cmd.extend(packages)
        return self._run(cmd)

    def remove(self, packages, **kw):
        if isinstance(packages, str):
            packages = [packages]

        extra_flags = kw.pop('extra_remove_flags', None)
        cmd = self.executable + ['--ignore-unknown', 'remove']
        if extra_flags:
            if isinstance(extra_flags, str):
                extra_flags = [extra_flags]
            cmd.extend(extra_flags)
        cmd.extend(packages)
        stdout, stderr, exitrc = self._check(
            cmd,
            **kw
        )
        # exitrc is 104 when package(s) not installed.
        if not exitrc in [0, 104]:
            raise RuntimeError("Failed to execute command: %s" % " ".join(cmd))
        return

    def clean(self):
        cmd = self.executable + ['refresh']
        return self._run(cmd)


class Pacman(PackageManager):
    """
    Pacman package management
    """

    executable = [
        'pacman',
        '--noconfirm',
    ]
    name = 'pacman'

    def install(self, packages, **kw):
        if isinstance(packages, str):
            packages = [packages]

        extra_flags = kw.pop('extra_install_flags', None)
        cmd = self.executable + [
            '-Sy',
        ]

        if extra_flags:
            if isinstance(extra_flags, str):
                extra_flags = [extra_flags]
            cmd.extend(extra_flags)
        cmd.extend(packages)
        return self._run(cmd)

    def remove(self, packages, **kw):
        if isinstance(packages, str):
            packages = [packages]

        extra_flags = kw.pop('extra_remove_flags', None)
        cmd = self.executable + [
            '-R'
        ]
        if extra_flags:
            if isinstance(extra_flags, str):
                extra_flags = [extra_flags]
            cmd.extend(extra_flags)

        cmd.extend(packages)
        return self._run(cmd)

    def clean(self):
        cmd = self.executable + ['-Syy']
        return self._run(cmd)

    def add_repo_gpg_key(self, url):
        cmd = ['pacman-key', '-a', url]
        self._run(cmd)


class AptRpm(PackageManager):
    """
    Apt-Rpm package management
    """

    executable = [
        'apt-get',
        '-y',
        '-q',
        '-V',
    ]
    name = 'apt'

    def install(self, packages, **kw):
        if isinstance(packages, str):
            packages = [packages]

        extra_flags = kw.pop('extra_install_flags', None)
        cmd = self.executable + ['install']

        if extra_flags:
            if isinstance(extra_flags, str):
                extra_flags = [extra_flags]
            cmd.extend(extra_flags)
        cmd.extend(packages)
        return self._run(cmd)

    def remove(self, packages, **kw):
        if isinstance(packages, str):
            packages = [packages]


        extra_flags = kw.pop('extra_remove_flags', None)
        cmd = self.executable + [
            '-y',
            'remove'
        ]
        if extra_flags:
            if isinstance(extra_flags, str):
                extra_flags = [extra_flags]
            cmd.extend(extra_flags)

        cmd.extend(packages)
        return self._run(cmd)

    def clean(self):
        cmd = self.executable + ['update']
        return self._run(cmd)


class Swupd(PackageManager):
    """
    Swupd package manager from ClearLinux distribution
    """

    executable = [
        'swupd',
        ]
    name = 'swupd'

    def install(self, packages, **kw):
        if isinstance(packages, str):
            packages = [packages]

        extra_flags = kw.pop('extra_install_flags', None)
        # Prior to installing packages, make sure we create folders for ceph
        # config and logging which do not exist in Clear
        cmd = ['mkdir', '-p', '/etc/ceph/']
        self._run(cmd)
        cmd = ['mkdir', '-p', '/var/log/ceph/']
        self._run(cmd)

        cmd = self.executable + ['bundle-add']

        if extra_flags:
            if isinstance(extra_flags, str):
                extra_flags = [extra_flags]
            cmd.extend(extra_flags)
        cmd.extend(packages)
        return self._run(cmd)

    def remove(self, packages, **kw):
        if isinstance(packages, str):
            packages = [packages]

        extra_flags = kw.pop('extra_remove_flags', None)
        cmd = self.executable + ['bundle-remove']

        if extra_flags:
            if isinstance(extra_flags, str):
                extra_flags = [extra_flags]
            cmd.extend(extra_flags)
        cmd.extend(packages)
        return self._run(cmd)

    def clean(self):
        cmd = self.executable + ['clean']
        return self._run(cmd)
