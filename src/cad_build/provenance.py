"""Record the Git checkout behind a build, without requiring Git to be available."""
import subprocess


def git_provenance(root):
    def git(*args):
        return subprocess.check_output(
            ['git', '-C', str(root), *args], stderr=subprocess.DEVNULL,
            timeout=5).decode('utf-8', errors='replace').strip()

    try:
        commit = git('rev-parse', 'HEAD')
        message = git('show', '-s', '--format=%B', commit)
        committed_at = git('show', '-s', '--format=%cI', commit)
        dirty = bool(git('status', '--porcelain', '--untracked-files=all'))
        if git('rev-parse', 'HEAD') != commit:
            return None
        return {'commit': commit, 'message': message, 'committed_at': committed_at,
                'dirty': dirty}
    except (OSError, subprocess.SubprocessError):
        return None
