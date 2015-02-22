import sys, os, subprocess
import configparser, itertools, json, re
import email.mime.text, email.utils, smtplib

class GitCommand:
    def __getattr__(self, name):
        def call(*args, capture_stderr = False, check = True):
            '''If <capture_stderr>, return stderr merged with stdout. Otherwise, return stdout and forward stderr to our own.
               If <check> is true, throw an exception of the process fails with non-zero exit code. Otherwise, do not.
               In any case, return a pair of the captured output and the exit code.'''
            cmd = ["git", name.replace('_', '-')] + list(args)
            with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT if capture_stderr else None) as p:
                (stdout, stderr) = p.communicate()
                assert stderr is None
                code = p.returncode
                if check and code:
                    raise Exception("Error running {0}: Non-zero exit code".format(cmd))
            return (stdout.decode('utf-8').strip('\n'), code)
        return call

git = GitCommand()
git_nullsha = 40*"0"

def git_is_forced_update(oldsha, newsha):
    out, code = git.merge_base("--is-ancestor", oldsha, newsha, check = False) # "Check if the first <commit> is an ancestor of the second <commit>"
    assert not out
    assert code in (0, 1)
    return False if code == 0 else True # if oldsha is an ancestor of newsha, then this was a "good" (non-forced) update

def read_config(fname, defSection = 'DEFAULT'):
    '''Reads a config file that may have options outside of any section.'''
    config = configparser.ConfigParser()
    with open(fname) as file:
        stream = itertools.chain(("["+defSection+"]\n",), file)
        config.read_file(stream)
    return config

def send_mail(subject, text, receivers, sender='post+webhook@ralfj.de', replyTo=None):
    assert isinstance(receivers, list)
    if not len(receivers): return # nothing to do
    # construct content
    msg = email.mime.text.MIMEText(text.encode('UTF-8'), 'plain', 'UTF-8')
    msg['Subject'] = subject
    msg['Date'] = email.utils.formatdate(localtime=True)
    msg['From'] = sender
    msg['To'] = ', '.join(receivers)
    if replyTo is not None:
        msg['Reply-To'] = replyTo
    # put into envelope and send
    s = smtplib.SMTP('localhost')
    s.sendmail(sender, receivers, msg.as_string())
    s.quit()

def get_github_payload():
    '''Reeturn the github-style JSON encoded payload (as if we were called as a github webhook)'''
    try:
        data = sys.stdin.buffer.read()
        data = json.loads(data.decode('utf-8'))
        return data
    except:
        return {} # nothing read

class Repo:
    def __init__(self, name, conf):
        '''Creates a repository from a section of the git-mirror configuration file'''
        self.name = name
        self.local = conf['local']
        self.owner = conf['owner'] # email address to notify in case of problems
        self.mirrors = {} # maps mirrors to their URLs
        mirror_prefix = 'mirror-'
        for name in filter(lambda s: s.startswith(mirror_prefix), conf.keys()):
            mirror = name[len(mirror_prefix):]
            self.mirrors[mirror] = conf[name]
    
    def mail_owner(self, msg):
        send_mail("git-mirror {0}".format(self.name), msg, [self.owner])
    
    def find_mirror_by_url(self, match_urls):
        for mirror, url in self.mirrors.items():
            if url in match_urls:
                return mirror
        return None
    
    def update_mirrors(self, ref, oldsha, newsha, except_mirrors = [], suppress_stderr = False):
        '''Update the <ref> from <oldsha> to <newsha> on all mirrors. The update must already have happened locally.'''
        assert len(oldsha) == 40 and len(newsha) == 40, "These are not valid SHAs."
        os.chdir(self.local)
        # check for a forced update
        is_forced = newsha != git_nullsha and oldsha != git_nullsha and git_is_forced_update(oldsha, newsha)
        # tell all the mirrors
        for mirror in self.mirrors:
            if mirror in except_mirrors:
                continue
            # update this mirror
            if is_forced:
                # forcibly update ref remotely (someone already did a force push and hence accepted data loss)
                git.push('--force', self.mirrors[mirror], newsha+":"+ref, capture_stderr = suppress_stderr)
            else:
                # nicely update ref remotely (this avoids data loss due to race conditions)
                git.push(self.mirrors[mirror], newsha+":"+ref, capture_stderr = suppress_stderr)
    
    def update_ref_from_mirror(self, ref, oldsha, newsha, mirror, suppress_stderr = False):
        '''Update the local version of this <ref> to what's currently on the given <mirror>. <oldsha> and <newsha> are checked. Then update all the other mirrors.'''
        os.chdir(self.local)
        url = self.mirrors[mirror]
        # first check whether the remote really is at newsha
        remote_state, code = git.ls_remote(url, ref)
        if remote_state:
            remote_sha = remote_state.split()[0]
        else:
            remote_sha = git_nullsha
        assert newsha == remote_sha, "Someone lied about the new SHA, which should be {0}.".format(newsha)
        # locally, we have to be at oldsha or newsha (the latter can happen if we already got this update, e.g. if it originated from us)
        local_state, code = git.show_ref(ref, check=False)
        if code == 0:
            local_sha = local_state.split()[0]
        else:
            if len(local_state):
                raise Exception("Something went wrong getting the local state of {0}.".format(ref))
            local_sha = git_nullsha
        assert local_sha in (oldsha, newsha), "Someone lied about the old SHA."
        # if we are already at newsha locally, we also ran the local hooks, so we do not have to do anything
        if local_sha == newsha:
            return
        # update local state from local_sha to newsha.
        if newsha != git_nullsha:
            # We *could* now fetch the remote ref and immediately update the local one. However, then we would have to
            # decide whether we want to allow a force-update or not. Also, the ref could already have changed remotely,
            # so that may update to some other commit.
            # Instead, we just fetch without updating any local ref. If the remote side changed in such a way that
            # <newsha> is not actually fetched, that's a race and will be noticed when updating the local ref.
            git.fetch(url, ref, capture_stderr = suppress_stderr)
            # now update the ref, checking the old value is still local_oldsha.
            git.update_ref(ref, newsha, 40*"0" if local_sha is None else local_sha)
        else:
            # ref does not exist anymore. delete it.
            assert local_sha != git_nullsha, "Why didn't we bail out earlier if there is nothing to do...?"
            git.update_ref("-d", ref, local_sha) # this checks that the old value is still local_sha
        # update all the mirrors
        self.update_mirrors(ref, oldsha, newsha, [mirror], suppress_stderr)

def find_repo_by_directory(repos, dir):
    for (name, repo) in repos.items():
        if dir == repo.local:
            return name
    return None

def load_repos():
    conffile = os.path.join(os.path.dirname(__file__), 'git-mirror.conf')
    conf = read_config(conffile)
    repos = {}
    for name, section in conf.items():
        if name != 'DEFAULT':
            repos[name] = Repo(name, section)
    return repos

