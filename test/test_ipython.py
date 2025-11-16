from traitlets.config.loader import Config as IPythonConfig
from IPython.terminal.embed import InteractiveShellEmbed
_ipython_cfg = IPythonConfig()
_ipython_cfg.TerminalInteractiveShell.banner1 = ""
_ipython_cfg.TerminalInteractiveShell.banner2 = ""
_ipython_cfg.TerminalInteractiveShell.exit_msg = ""
_ipython_cfg.TerminalInteractiveShell.confirm_exit = False
_ipython_cfg.TerminalInteractiveShell.automagic = False
ip_shell = InteractiveShellEmbed(config=_ipython_cfg)
ip_shell()
