# Main class for pycamps

import os
import stat
import sys
import re
import time
import shutil
import hashlib
import logging
import argparse
import ConfigParser

# import the rpm parsing stuff
import rpm

# koji can replace rpm above and do package building
import koji
import xmlrpclib

# GitPython
import git
from git.errors import InvalidGitRepositoryError, NoSuchPathError, GitCommandError

# settings, including lookaside uri and temporary paths
import skein_settings as sks
from gitremote import GitRemote

class SkeinError(Exception):

    def __init__(self, value):
        self.value = value

    def __str__(self):
        repr(self.value)

# Add a class stolen from /usr/bin/koji to watch tasks
# this was cut/pasted from koji, and then modified for local use.
# The formatting is koji style, not the stile of this file.  Do not use these
# functions as a style guide.
# This is fragile and hopefully will be replaced by a real kojiclient lib.
class TaskWatcher(object):

    def __init__(self,task_id,session,level=0,quiet=False):
        self.id = task_id
        self.session = session
        self.info = None
        self.level = level
        self.quiet = quiet

    #XXX - a bunch of this stuff needs to adapt to different tasks

    def str(self):
        if self.info:
            label = koji.taskLabel(self.info)
            return "%s%d %s" % ('  ' * self.level, self.id, label)
        else:
            return "%s%d" % ('  ' * self.level, self.id)

    def __str__(self):
        return self.str()

    def get_failure(self):
        """Print infomation about task completion"""
        if self.info['state'] != koji.TASK_STATES['FAILED']:
            return ''
        error = None
        try:
            result = self.session.getTaskResult(self.id)
        except (xmlrpclib.Fault,koji.GenericError),e:
            error = e
        if error is None:
            # print "%s: complete" % self.str()
            # We already reported this task as complete in update()
            return ''
        else:
            return '%s: %s' % (error.__class__.__name__, str(error).strip())

    def update(self):
        """Update info and log if needed.  Returns True on state change."""
        if self.is_done():
            # Already done, nothing else to report
            return False
        last = self.info
        self.info = self.session.getTaskInfo(self.id, request=True)
        if self.info is None:
            self.logger.error("No such task id: %i" % self.id)
            print "No such task id: %i" % self.id
            sys.exit(1)
        state = self.info['state']
        if last:
            #compare and note status changes
            laststate = last['state']
            if laststate != state:
                msg = "%s: %s -> %s" % (self.str(), self.display_state(last), self.display_state(self.info))
                self.logger.info(msg)
                print msg
                return True
            return False
        else:
            # First time we're seeing this task, so just show the current state
            self.logger.info("%s: %s" % (self.str(), self.display_state(self.info)))
            print "%s: %s" % (self.str(), self.display_state(self.info))
            return False

    def is_done(self):
        if self.info is None:
            return False
        state = koji.TASK_STATES[self.info['state']]
        return (state in ['CLOSED','CANCELED','FAILED'])

    def is_success(self):
        if self.info is None:
            return False
        state = koji.TASK_STATES[self.info['state']]
        return (state == 'CLOSED')

    def display_state(self, info):
        # We can sometimes be passed a task that is not yet open, but
        # not finished either.  info would be none.
        if not info:
            return 'unknown'
        if info['state'] == koji.TASK_STATES['OPEN']:
            if info['host_id']:
                host = self.session.getHost(info['host_id'])
                return 'open (%s)' % host['name']
            else:
                return 'open'
        elif info['state'] == koji.TASK_STATES['FAILED']:
            return 'FAILED: %s' % self.get_failure()
        else:
            return koji.TASK_STATES[info['state']].lower()

class PySkein:
    """
    Support class for skein. Does single and mass imports, upload, verify, sources, 
    generate makefiles and more for the goose linux rebuilds.
    """
    
    def __init__(self):

        self.username = None

        config = ConfigParser.SafeConfigParser()
        f = open('/etc/skein/skein.cfg')
        config.readfp(f)
        f.close()

        self.cfgs = {}

        for section in config.sections():
            self.cfgs[section] = {}
            for k, v in config.items(section):
                self.cfgs[section][k] = v

        self._makedir(self.cfgs['skein']['install_root'])

        # create logger with 'spam_application'
        self.logger = logging.getLogger('skein')
        self.logger.setLevel(eval(self.cfgs['logger']['loglevel']))

        # create file handler which logs even debug messages
        fh = logging.FileHandler(self.cfgs['logger']['file'])
        fh.setLevel(eval(self.cfgs['logger']['loglevel']))

        # create formatter and add it to the handlers
        formatter = logging.Formatter(self.cfgs['logger']['format'])
        fh.setFormatter(formatter)
        # add the handlers to the logger
        self.logger.addHandler(fh)

    def _makedir(self, target, perms=0775):
        if not os.path.isdir(u"%s" % (target)):
            os.makedirs(u"%s" % (target), perms)

    def _init_koji(self, user=None, kojiconfig=None, url=None):
        """Initiate a koji session.  Available options are:

        user: User to log nto koji as (if no user, no login)

        kojiconfig: Use an alternate koji config file

        This function attempts to log in and returns nothing or raises.

        """

        # Code from /usr/bin/koji. Should be in a library!
        defaults = {
                    'server' : 'http://localhost/kojihub',
                    'weburl' : 'http://localhost/koji',
                    'pkgurl' : 'http://localhost/packages',
                    'topdir' : '/mnt/koji',
                    'cert': '~/.koji/client.crt',
                    'ca': '~/.koji/clientca.crt',
                    'serverca': '~/.koji/serverca.crt',
                    'authtype': None
                    }
        # Process the configs in order, global, user, then any option passed
        configs = ['/etc/koji.conf', os.path.expanduser('~/.koji/config')]
        if kojiconfig:
            configs.append(os.path.join(kojiconfig))
        for configFile in configs:
            if os.access(configFile, os.F_OK):
                f = open(configFile)
                config = ConfigParser.ConfigParser()
                config.readfp(f)
                f.close()
                if config.has_section('koji'):
                    for name, value in config.items('koji'):
                        if defaults.has_key(name):
                            defaults[name] = value
        # Expand out the directory options
        for name in ('topdir', 'cert', 'ca', 'serverca'):
            defaults[name] = os.path.expanduser(defaults[name])
        session_opts = {'user': user}
        # We assign the kojisession to our self as it can be used later to
        # watch the tasks.
        self.logger.debug('Initiating a koji session to %s' % defaults['server'])
        try:
            if user:
                self.kojisession = koji.ClientSession(defaults['server'],
                                                      session_opts)

                self.logger.debug('Logged into a koji session to %s as %s' % (defaults['server'], user ))
            else:
                self.kojisession = koji.ClientSession(defaults['server'])
        except:
            raise SkeinError('Could not initiate koji session')
        # save the weburl for later use too
        self.kojiweburl = defaults['weburl']
        self.logger.debug('Kojiweb URL: %s' % self.kojiweburl)
        # log in using ssl
        if user:
            try:
                self.kojisession.ssl_login(defaults['cert'], defaults['ca'],
                                           defaults['serverca'])
            except:
                raise SkeinError('Opening a SSL connection failed')
            if not self.kojisession.logged_in:
                raise SkeinError('Could not auth with koji as %s' % user)
        return

    # grab the details from the rpm and add them to the object
    def _set_srpm_details(self, srpm):

        self.logger.info("== Querying srpm ==")
        ts = rpm.ts()
        ts.setVSFlags(rpm._RPMVSF_NOSIGNATURES)
    
        fdno = open(u"%s" % srpm, 'r')
        try:
            hdr = ts.hdrFromFdno(fdno)
        except rpm.error, e:
            if str(e) == "public key not available":
                print str(e)
        fdno.close()
        
        self.logger.info("  Setting srpm name ==")
        self.name = hdr[rpm.RPMTAG_NAME]
        self.logger.info("  Setting srpm version ==")
        self.version = hdr[rpm.RPMTAG_VERSION]
        self.logger.info("  Setting srpm release ==")
        self.release = hdr[rpm.RPMTAG_RELEASE]
        self.logger.info("  Setting srpm sources ==")
        self.sources = hdr[rpm.RPMTAG_SOURCE]
        self.logger.info("  Setting srpm patches ==")
#        print "rpm patches: %s" % hdr[rpm.RPMTAG_PATCH]
        self.patches = []
        for patch in hdr[rpm.RPMTAG_PATCH]:
            self.patches.append(patch.replace('%{name}', self.name))
#        self.patches = hdr[rpm.RPMTAG_PATCH].replace('%{name}', self.name)
        self.logger.info("  Setting srpm summary ==")
        self.summary = hdr[rpm.RPMTAG_SUMMARY]
        self.logger.info("  Setting srpm url ==")
        self.url = hdr[rpm.RPMTAG_URL]
        self.logger.info("  Setting srpm requires ==")
        # note to self, the [:-2] strips off the rpmlib(FileDigests)' and 
        #'rpmlib(CompressedFileNames)' which are provided by the 'rpm' rpm
        self.buildrequires = hdr[rpm.RPMTAG_REQUIRES]

    # install the srpm in a temporary directory
    def _install_srpm(self, srpm):
        # rpm.ts is an alias for rpm.TransactionSet
        self.logger.info("== Installing srpm ==")
    
        self._makedir(u"%s/%s" % (sks.install_root, self.name))
    
        self.logger.info("  installing %s into %s/%s" % (srpm, sks.install_root, self.name))
        args = ["/bin/rpm", "-i", "--root=%s/%s" % (sks.install_root, self.name), "%s" % (srpm)]
        p = subprocess.call(args, stdout = subprocess.PIPE, stderr = subprocess.PIPE )

    def _copy_sources(self, sources_src, sources_dest):
        self.logger.info("== Copying sources ==")
        # copy the source files
        for source in self.sources:
        #    print "source: %s/%s" % (sources_src, source)
            self.logger.info("  %s to %s" % (source, sources_dest))
            shutil.copy2("%s/%s" % (sources_src, source), sources_dest)
    
    # this method assumes the sources are new and overwrites the 'sources' file in the git repository
    def _generate_sha256(self, sources_dest, spec_dest):
        self.logger.info("== Generating sha256sum for sources ==")
        sfile = open(u"%s/sources" % spec_dest, 'w+')
        for source in self.sources:
            sha256sum = hashlib.sha256(open(u"%s/%s" % (sources_dest, source), 'rb').read()).hexdigest()
            sfile.write(u"%s *%s\n" % (sha256sum, source))
        #close the file
        sfile.close()

        self.logger.info("  sha256sums generated and added to %s/sources" % spec_dest)

    def _copy_spec(self, spec_src, spec_dest):
        self.logger.info("== Copying spec ==")

        # copy the spec file
        self.logger.info("  %s.spec to %s" % (self.name, spec_dest))
        shutil.copy2(spec_src, spec_dest)

    def _copy_patches(self, patches_src, patches_dest):
        self.logger.info("== Copying patches ==")
        # copy the patch files
        #print "patches: %s" % self.patches
        for patch in self.patches:
            self.logger.info("  %s to %s" % (patch, patches_dest))
            shutil.copy2("%s/%s" % (patches_src, patch), patches_dest)


    # create a git repository pointing to appropriate github repo
    def _clone_git_repo(self, repo_dir, scm_url):
        self.logger.info("== Creating local git repository at '%s' ==" % repo_dir)

        try:
            self.repo = git.Repo(repo_dir)
        except InvalidGitRepositoryError, e:
            gitrepo = git.Git(repo_dir)
            cmd = ['git', 'init']
            result = git.Git.execute(gitrepo, cmd)
            self.repo = git.Repo(repo_dir)

        self.logger.info("  Performing git pull from origin at '%s'" % scm_url)

        try:
            self.repo.create_remote('origin', scm_url)
            self.repo.remotes['origin'].pull('refs/heads/master:refs/heads/master')
        except (AssertionError, GitCommandError), e:
            self.logger.debug("--- Exception thrown %s" % e)
            origin = self.repo.remotes['origin']
            reader = origin.config_reader
            url = reader.get("url")
            if not url == scm_url:
                self.logger.info(u"  origin is %s, should be %s. Adjusting" % (url, scm_url))
                try:
                    self.repo.delete_remote('old_origin')
                except GitCommandError, e:
                    origin.rename('old_origin')
                    self.repo.create_remote('origin', scm_url)
                    self.repo.remotes['origin'].pull('refs/heads/master:refs/heads/master')
                    
    # attribution to fedpkg, written by 'Jesse Keating' <jkeating@redhat.com> for this snippet
    def _update_gitignore(self, path):
        self.logger.info("  Updating .gitignore with sources")
        gitignore_file = open("%s/%s" % (path, '.gitignore'), 'w')
        for line in self.sources:
            gitignore_file.write("%s\n" % line)
        gitignore_file.close()

    # search for a makefile.tpl in the makefile_path and use
    # it as a template to put in each package's repository
    def _do_makefile(self):
        self.logger.info("  Updating Makefile")
        found = False
        for path in sks.makefile_path.split(':'):
            expanded_path = "%s/%s" % (os.path.expanduser(path), sks.makefile_name)
#            print "expanded_path: %s" % expanded_path
            if os.path.exists(expanded_path):
                makefile_template = expanded_path
                found = True
                break

        if not found:
            self.logger.error("'%s' not found in path '%s', please fix in the skein_settings.py" % (sks.makefile_name, sks.makefile_path))
            raise IOError("'%s' not found in path '%s', please fix in the skein_settings.py" % (sks.makefile_name, sks.makefile_path))

#        print "makefile template found at %s" % makefile_template

        src_makefile = open(makefile_template)
        dst_makefile = open("%s/%s/Makefile" % (sks.base_dir, self.name), 'w')

        dst_makefile.write( src_makefile.read() % {'name': self.name})
        dst_makefile.close()

    def _upload_sources(self, sources_path):

        self.logger.info("== Uploading Sources ==")
#        os.chdir( sks.lookaside_dir  )
#        print "CWD: %s" % os.getcwd()
#        print "PKG: %s" % self.name

        for source in self.sources:
#            print "rsync -vloDtRz -e ssh %s/%s %s@%s:%s/" % (self.name, source, sks.lookaside_user, sks.lookaside_host, sks.lookaside_remote_dir)

            self.logger.info("  uploading %s to %s" % (source, sks.lookaside_host))
            args = ["/usr/bin/rsync", "-loDtRz", "-e", "ssh", "%s/%s" % (self.name, source), "%s@%s:%s/" % ( sks.lookaside_user, sks.lookaside_host, sks.lookaside_remote_dir)]
            p = subprocess.call(args, cwd="%s" % (sks.lookaside_dir), stdout = subprocess.PIPE)
#            os.waitpid(p.pid, 0)
#            print "result %s" % p.communicate()[0]
#            time.sleep(2)

    def _commit_and_push(self, repo=None):

        self.logger.info("== Committing and pushing git repo ==")
        if not repo:
            repo = self.repo

        index = repo.index

        self.logger.info("  adding updated files to the index")
        index_changed = False
        if repo.is_dirty():
           #print "index: %s" % index
#            for diff in index.diff(None):
#                print diff.a_blob.path

            index.add([diff.a_blob.path.rstrip('\n') for diff in index.diff(None)])
            index_changed = True

        self.logger.info("  adding untracked files to the index") 
        # add untracked files
        path = os.path.split(sks.base_dir)[0]
        #print "path: %s" % path
        if repo.untracked_files:
#            print "untracked files: %s" % repo.untracked_files
            index.add(repo.untracked_files)
            index_changed = True

        if index_changed:
            self.logger.info("  committing index")
            # commit files added to the index
            index.commit(sks.commit_message)

        self.logger.info(" Pushing '%s' to '%s'" % (self.name, sks.git_remote))
        try:
            self.repo.remotes['origin'].push('refs/heads/master:refs/heads/master')
        except IndexError, e:
            print "--- Push failed with error: %s ---" % e
            self.logger.debug("--- Push failed with error: %s" % e)
            raise
        except AssertionError, e:
            # odds are that unless the exception 'e' has a value
            # the assertionerror is wrong.  Usually, this is because
            # gitPython shows a warning, not an actual error
            if e and len(str(e)) != 0:
                print "--- Push failed with error: %s ---" % e
                self.logger.debug("--- Push failed with error: %s" % e)
                raise 

    def _get_srpm_list(self, path):

        if os.path.isdir(path):
            return os.listdir(path)
        elif os.path.isfile(path):
            return [path]
        else:
            print "'%s' is not valid" % path
            sys.exit(1)

    def _watch_koji_tasks(self, session, tasklist, quiet=False):
        if not tasklist:
            return
        self.logger.info('Watching tasks (this may be safely interrupted)...')
        print 'Watching tasks (this may be safely interrupted)...'
        # Place holder for return value
        rv = 0
        try:
            tasks = {}
            for task_id in tasklist:
                tasks[task_id] = TaskWatcher(task_id, session, quiet=quiet)
            while True:
                all_done = True
                for task_id,task in tasks.items():
                    changed = task.update()
                    if not task.is_done():
                        all_done = False
                    else:
                        if changed:
                            # task is done and state just changed
                            if not quiet:
                                pass
                                #_display_tasklist_status(tasks)
                        if not task.is_success():
                            rv = 1
                    for child in session.getTaskChildren(task_id):
                        child_id = child['id']
                        if not child_id in tasks.keys():
                            tasks[child_id] = TaskWatcher(child_id, session, task.level + 1, quiet=quiet)
                            tasks[child_id].update()
                            # If we found new children, go through the list again,
                            # in case they have children also
                            all_done = False
                if all_done:
                    if not quiet:
                        print
                        #_display_task_results(tasks)
                    break

                time.sleep(1)
        except (KeyboardInterrupt):
            if tasks:
                kbd_msg = """\nTasks still running. You can continue to watch with the 'koji watch-task' command.  Running Tasks: %s""" % '\n'.join(['%s: %s' % (t.str(), t.display_state(t.info)) for t in tasks.values() if not t.is_done()])
                self.logger.info(kbd_msg)
                print kbd_msg

            # /us/rbin/koji considers a ^c while tasks are running to be a
            # non-zero exit.  I don't quite agree, so I comment it out here.
            #rv = 1
        return rv

    def _init_git_remote(self):

        remoteClassName = self.cfgs['git']['remote_class']
        remoteModuleName = self.cfgs['git']['remote_module']

        try:
            remoteModule = __import__(remoteModuleName,
                                      globals(),
                                      locals(),
                                      [remoteClassName])
            self.gitremote = GitRemote(remoteModule.__dict__[remoteClassName], self.cfgs, self.logger)
        except ImportError, e:
            print "Remote class %s in module %s not found" % (remoteClassName,
                                                              remoteModuleName)

    def request_remote_repo(self, args):
        self._init_git_remote()
        return self.gitremote.request_remote_repo(args.name, args.reason)

    def search_repo_requests(self, args):
        self._init_git_remote()
        state = 'open'

        if args.state:
            state = 'closed'

        return self.gitremote.search_repo_requests(state=state)

    def show_request_by_id(self, args):
        self._init_git_remote()

        name, summary, url, owner = self.gitremote.show_request_by_id(args.id)

        print "\nDetails for request # %s, requested by: %s" % (args.id, owner)
        print "-------------------------"
        print "Package Name: %s" % name
        print "Package Summary: %s" % summary
        print "Package URL: %s\n" % url

    def _enable_pkg(self, name, summary, url, owner, tag=None):

        if not tag:
            tag = self.cfgs['koji']['latest_tag']

        self.gitremote.create_remote_repo(name, summary, url)
        self.gitremote.create_team("%s_%s" % (self.cfgs['skein']['team_prefix'], name), 'admin', [name])

        try:
            if not self.kojisession.checkTagPackage(tag, name):
                self.kojisession.packageListAdd(tag, name, owner=owner)
                self.logger.info("== Added package '%s' to the tag '%s'" % (name, tag))
                print "Added package '%s' to the tag '%s'" % (name, tag)
            else:
                self.logger.info("== Package '%s' already added to tag '%s'" % (name, tag))
                print "Package '%s' already added to tag '%s', skipping" % (name, tag)

        except (xmlrpclib.Fault,koji.GenericError),e:
            raise SkeinError("Unable to tag package %s due to error: %s" % (name, e))

#    def create_team(self, args):
#
#        self._init_git_remote()
#        self.gitremote.create_team('testTeamA', 'admin', ['gooselinux/wavpack'])

    def grant_request(self, args):

        self._init_git_remote()

        tag = None
        if args.tag:
            tag = args.tag

        name, summary, url, owner = self.gitremote.show_request_by_id(args.id)

        if args.owner:
            owner = args.owner

        print "Name: %s\nSummary: %s\nURL: %s\n" % (name, summary, url)
        valid = 'n'
        valid = raw_input("Is the above information correct? (y/N) ")

        if valid.lower() == 'y':
            kojiconfig = None
            if args.config:
                kojiconfig = args.config

            self._init_koji(user=self.cfgs['koji']['username'], kojiconfig=kojiconfig)
            self._enable_pkg(name, summary, url, owner, tag)

    def do_build_pkg(self, args):

        kojiconfig = None
        if args.config:
            kojiconfig = args.config

        self._init_koji(user=self.cfgs['koji']['username'], kojiconfig=kojiconfig)
        build_target = self.kojisession.getBuildTarget(args.target)

        #print "Args.Target: %s" % args
        #print "Build Target: %s" % build_target

        if not build_target:
            raise SkeinError('Unknown build target: %s' % args.target)

        dest_tag = self.kojisession.getTag(build_target['dest_tag_name'])
        #print "Dest Tag: %s" % dest_tag

        if not dest_tag:
            raise SkeinError('Unknown destination tag %s' %
                              build_target['dest_tag_name'])

        if dest_tag['locked']:
            raise SkeinError('Destination tag %s is locked' % dest_tag['name'])

        opts = {}
        priority = 5

        task_id = self.kojisession.build('git://github.com/gooselinux/%s.git#HEAD' % args.name, args.target, opts, priority=priority)

        #print "Task-ID: %s" % task_id
        print "Task URL: %s/%s?taskID=%s" % ('http://koji.gooselinux.org/koji', 'taskinfo', task_id) 

        self._watch_koji_tasks(self.kojisession, [task_id])

        self.kojisession.logout()


    def list_deps(self, args):

        path = args.path
        srpms = self._get_srpm_list(path)

        for srpm in srpms:
            self._set_srpm_details(u"%s" % (srpm))
            print "== Deps for %s ==" % (srpm)
            self.logger.info("== Getting deps for %s==" % (srpm))
            for br in self.buildrequires:
                self.logger.info("  %s" % br)
                print "  %s" % br
            print ""

    def do_import(self, args):

#        path = args.path
#        print "PATH: %s" % args.path
#        print "
        for path in args.path:
            srpms = self._get_srpm_list(path)
        
            for srpm in srpms:
                print "Importing %s" % (srpm)
                self.logger.info("== Importing %s==" % (srpm))
                self._set_srpm_details(u"%s" % (srpm))
                self._install_srpm(u"%s" % (srpm))
    
                # make sure the github repo exists
                self._create_remote_repo()
                time.sleep(1)
    
                spec_src = u"%s/%s%s/%s/%s.spec" % (sks.install_root, self.name, sks.home, 'rpmbuild/SPECS', self.name)
                spec_dest = u"%s/%s" % (sks.base_dir, self.name)
                sources_src = u"%s/%s%s/%s" % (sks.install_root, self.name, sks.home, 'rpmbuild/SOURCES')
                sources_dest = u"%s/%s" % (sks.lookaside_dir, self.name)
    
    #            print "spec_src: %s" % spec_src
    #            print "spec_dest: %s" % spec_dest
    #            print "sources_src: %s" % sources_src
    #            print "sources_dest: %s" % sources_dest
    
                self._makedir(spec_dest)
                self._clone_git_repo(spec_dest, u"%s/%s.git" %(sks.git_remote, self.name))
    
                self._copy_spec(spec_src, spec_dest)
                self._copy_patches(sources_src, spec_dest)
    
                self._makedir(sources_dest)
                self._copy_sources(sources_src, sources_dest)
                self._generate_sha256(sources_dest, spec_dest)
    
                self._update_gitignore(spec_dest)
    
                self._do_makefile()
                if not args.no_upload:
                    self._upload_sources(sources_dest)
    
                if not args.no_push:
                    self._commit_and_push()
    
                print "Import %s complete\n" % (self.name)
                self.logger.info("== Import of '%s' complete ==\n" % (srpm))

def main():

    ps = PySkein()


    p = argparse.ArgumentParser(
            description='''Imports all src.rpms into git and lookaside cache''',
        )



#    p.add_argument("name", help=u"id of new repo request being created")
    p.set_defaults(func=ps.create_team)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    sys.exit(main())



