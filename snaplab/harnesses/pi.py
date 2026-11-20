"""Pi uses exactly the remote worker isolation, transfer and cancellation path."""
from .base import Harness
from . import remote


class PiHarness(Harness):
    id = 'pi'
    label = {'en': 'Pi', 'ja': 'Pi', 'ru': 'Pi'}
    description = {'en': 'Coding and research in an isolated Linux workspace.',
                   'ja': 'Linux の囲いの中でコードの作成や資料の調査を行います。',
                   'ru': 'Разработка и исследование в изолированной среде Linux.'}
    runs_code = True
    sandboxed = True
    icon = 'terminal'

    def available(self):
        if remote.backend(self.app) == 'wsl':
            from . import wsl
            return wsl.available(self.app)
        if remote.backend(self.app) == 'none':
            return False, 'the SnapCode workspace is not set up on this PC yet (see docs/linux.md)'
        if remote.backend(self.app) != 'ssh':
            return False, 'Pi requires a remote Linux worker'
        return remote.available(self.app)

    def run_turn(self, session, folder, prompt, ctl, first):
        if remote.backend(self.app, session) == 'wsl':
            from . import wsl
            return wsl.run_turn(self.app, session, folder, prompt, ctl)
        if remote.backend(self.app, session) != 'ssh':
            raise ValueError('Pi requires a remote Linux worker')
        return remote.run_turn(self.app, session, folder, prompt, ctl)
