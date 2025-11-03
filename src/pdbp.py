"""
pdbp (Pdb+): A drop-in replacement for pdb and pdbpp.
=====================================================
"""
import code
import codecs
import inspect
import math
import os
import pprint
import re
import shutil
import signal
import sys
import traceback
import types
from collections import OrderedDict
from inspect import signature
import io
from io import StringIO
from tabcompleter import Completer, ConfigurableClass, Color
import tabcompleter
import _thread
import threading
import subprocess
import termios
import pty
import tty
import atexit
import time
import objprint
import stat

# To ensure the Python readline hook go first
import readline  
if not os.environ.get("_PDB_W_MT", ""):
    import csrc._rl_patch as _rl_patch
else:
    import csrc._rl_patch_mt as _rl_patch

try:
    from pygments.styles.zenburn import ZenburnStyle 
    from pygments.token import Keyword, Name, Comment, String, Error, Number, Operator, Generic, Token, Literal, Punctuation

    class DesertStyle(ZenburnStyle):
        styles = {
            **ZenburnStyle.styles,
            Token.Text: '#ffffff',

            Keyword: '#d7d787 bold',
            Keyword.Type: '#87ff87 nobold',
            Keyword.Constant: '#87ff87 nobold',
            Keyword.Declaration: '#d7d787 bold',
            Keyword.Namespace: '#d75f5f nobold',
            Keyword.Reserved: '#d7d787 bold',
            Keyword.Pseudo: '#d7d787 nobold',

            Name: '#ffffff',
            Name.Class: '#afaf5f bold',
            Name.Function: '#87ff87',
            Name.Builtin: '#87ff87',
            Name.Builtin.Pseudo: '#87ff87',
            Name.Exception: '#afaf5f bold',
            Name.Decorator: '#87ff87',

            Literal: '#ffafaf',

            String: '#ffafaf',
            String.Doc: '#ffafaf',
            String.Interpol: '#ffffff',

            Number: '#ffafaf',
            Number.Float: '#ffafaf',

            Operator: '#ffffff',

            Punctuation: '#ffffff',
    
            Comment: '#5fd7ff',
            Comment.Multiline: '#ffafaf',
            
            # For IPython
            Token.Prompt: '#ffffff', 
            Token.PromptNum: '#87ff87',
            Token.OutPrompt: '#ffffff',
            Token.OutPromptNum: '#ffafaf',
        }

except ImportError:
    DesertStyle = "zenburn"


__url__ = "https://github.com/mdmintz/pdbp"
__version__ = tabcompleter.LazyVersion("pdbp")
run_from_main = False

# Digits, Letters, [], or Dots
side_effects_free = re.compile(r"^ *[_0-9a-zA-Z\[\].]* *$")
_pdb_lock = threading.Lock()
_readline_lock = threading.Lock()
_inject_lock = threading.Lock()
_ipython_enabled = False
_ipython_nested = False
_ipython_cfg = None


def import_from_stdlib(name):
    result = types.ModuleType(name)
    stdlibdir, _ = os.path.split(code.__file__)
    pyfile = os.path.join(stdlibdir, name + ".py")
    with open(pyfile) as f:
        src = f.read()
    co_module = compile(src, pyfile, "exec", dont_inherit=True)
    exec(co_module, result.__dict__)
    return result

pdb = import_from_stdlib("pdb")


def rebind_globals(func, newglobals):
    newfunc = types.FunctionType(func.__code__, newglobals, func.__name__,
                                 func.__defaults__, func.__closure__)
    return newfunc


def is_char_wide(char):
    # Returns True if the char is Chinese, Japanese, Korean, or another double.
    special_c_r = [
        {"from": ord("\u4e00"), "to": ord("\u9FFF")},
        {"from": ord("\u3040"), "to": ord("\u30ff")},
        {"from": ord("\uac00"), "to": ord("\ud7a3")},
        {"from": ord("\uff01"), "to": ord("\uff60")},
    ]
    sc = any(
        [range["from"] <= ord(char) <= range["to"] for range in special_c_r]
    )
    return sc


def get_width(line):
    # Return the true width of the line. Not the same as line length.
    # Chinese/Japanese/Korean characters take up two spaces of width.
    line_length = len(line)
    for char in line:
        if is_char_wide(char):
            line_length += 1
    return line_length


def set_line_width(line, width, tll=True):
    """Trim line if too long. Fill line if too short. Return line."""
    line_width = get_width(line)
    new_line = ""
    width = int(width)
    if width <= 0:
        return new_line
    elif line_width == width:
        return line
    elif line_width < width:
        new_line = line
    else:
        for char in line:
            updated_line = "%s%s" % (new_line, char)
            if get_width(updated_line) > width:
                break
            new_line = updated_line
    extra_spaces = ""
    if tll:
        extra_spaces = " " * (width - get_width(new_line))
    return "%s%s" % (new_line, extra_spaces)


class DefaultConfig(object):
    if "win32" in sys.platform:
        import colorama
        colorama.just_fix_windows_console()
    prompt = "(Pdb+) "
    highlight = True
    sticky_by_default = False
    bg = "dark"
    use_pygments = True
    colorscheme = None
    style = DesertStyle
    use_terminal256formatter = True  # Defaults to `"256color" in $TERM`.
    editor = "${EDITOR:-vim} +<lineno> <filename>"  # Use $EDITOR if set; else default to vim.
    ipython_editor = "vim"
    stdin_paste = None
    exec_if_unfocused = None  # This option was removed!
    truncate_long_lines = False
    shorten_path = True
    disable_pytest_capturing = True
    enable_hidden_frames = False
    show_hidden_frames_count = False
    encodings = ("utf-8", "latin-1")
    filename_color = "38;5;167"
    line_number_color = "38;5;226" 
    regular_stack_color = "38;5;120" 
    pm_stack_color = "38;5;217"
    stack_color = regular_stack_color
    # https://en.wikipedia.org/wiki/ANSI_escape_code#3-bit_and_4-bit
    return_value_color = "38;5;231;1"  # Gray
    pm_return_value_color = return_value_color  # Red (Post Mortem failure)
    num_return_value_color = return_value_color  # Bright Magenta (numbers)
    true_return_value_color = return_value_color  # Green
    false_return_value_color = return_value_color  # Yellow (red was taken)
    none_return_value_color = return_value_color  # Yellow (same as False)
    regular_line_color = "97;48;5;67;1"  # White on Blue (Old: "39;49;7")
    pm_cur_line_color = "97;48;5;133;1"  # White on Red (Post Mortem Color)
    exc_line_color = "38;5;16;48;5;144"  # Red on Yellow (Exception-handling)
    current_line_color = regular_line_color
    exception_caught = False
    last_return_color = None
    show_traceback_on_error = True
    show_traceback_on_error_limit = None
    default_pdb_kwargs = {
    }
    post_mortem_restart = False
    external_print_tmp_dir = "~/.pdbp_cache"
    external_print_prefix = "eval"
    external_print_postfix = ".py"
    external_print_cache_limit = -1
    external_print_cmd = "vim -c 'term ++close ++curwin less -R <filename>'"
    external_print_subfix = "_sub"

    def setup(self, pdb):
        pass

    def before_interaction_hook(self, pdb):
        pass


def setbgcolor(line, color):
    # Add a bgcolor attribute to all escape sequences found.
    setbg = "\x1b[%sm" % color
    regexbg = "\\1;%sm" % color
    result = setbg + re.sub("(\x1b\\[.*?)m", regexbg, line) + "\x1b[00m"
    if os.environ.get("TERM") == "eterm-color":
        result = result.replace(setbg, "\x1b[37;%dm" % color)
        result = result.replace("\x1b[00;%dm" % color, "\x1b[37;%dm" % color)
        result = result.replace("\x1b[39;49;00;", "\x1b[37;")
    return result


CLEARSCREEN = "\033[2J\033[1;1H"


def lasti2lineno(code, lasti):
    import dis
    linestarts = list(dis.findlinestarts(code))
    linestarts.reverse()
    for i, lineno in linestarts:
        if lasti >= i:
            return lineno
    return 0


class Restart(Exception):
    pass


class Undefined:
    def __repr__(self):
        return "<undefined>"


undefined = Undefined()


_inject = {}
exec(
    compile(
        """
def _inject_wrapper(fn):
    from functools import wraps
    @wraps(fn)
    def new_fn(*args, **kwargs):
        import pdbp; pdbp.set_trace()
        return fn(*args, **kwargs)
    return new_fn
        """,
        "<_dynamic_>",
        "exec",
    ),
    _inject,
)
_inject = _inject["_inject_wrapper"]
_inject_handles = {}
_inject_id =  0


class _InjectHandle:
    def __init__(self, context, clb: str, _id: int):
        if not hasattr(context, clb):
            print(f"Attribute {clb} not found in {context}, nothing happens")
            return
        clb_ins = getattr(context, clb)
        self.context = context
        self.clb_name = clb
        self.old_clb = clb_ins
        self.id = _id
        setattr(self.context, clb, _inject(clb_ins))
        _inject_handles[self.id] = self
        print(f"<No.{_id}> Register debug hook for {clb} in {context}")

    def remove(self):
        setattr(self.context, self.clb_name, self.old_clb)
        _inject_handles.pop(self.id)
        print(f"<No.{self.id}> Detach debug hook for {self.clb_name} in {self.context}")

    def __repr__(self):
        context_name = getattr(self.context, "__name__", "<no name>")
        return f"{context_name}.{self.clb_name}"


def inject_debug(context, clb):
    _inject_lock.acquire()
    global _inject_id
    hook = _InjectHandle(context, clb, _inject_id)
    _inject_id += 1
    _inject_lock.release()


def remove_debug(id_num):
    _inject_lock.acquire()
    if id_num not in _inject_handles:
        print(f"The hook <No.{id_num}> does not exist, use `pdbp.show_debug()` to get available numbers")
        return
    _inject_handles[id_num].remove()
    _inject_lock.release()


def show_debug():
    _inject_lock.acquire()
    pprint.pp(_inject_handles)
    _inject_lock.release()


def _new_thread_run(self):
    rt = _ori_thread_run(self)
    
    if _thread.get_native_id() not in _thread_list:
        return rt

    global GLOBAL_PDB
    if GLOBAL_PDB is not None:
        GLOBAL_PDB._cleanup()
   
    try:
        _pdb_lock.release()
    except RuntimeError:
        pass

    try:
        _readline_lock.release()
    except RuntimeError:
        pass
    
    try:
        _inject_lock.release()
    except RuntimeError:
        pass

    return rt

if __name__ != "__main__":
    _thread_list = []
    _ori_thread_run = threading.Thread.run
    threading.Thread.run = _new_thread_run 
    _atexit_registered = 0
        

class _TLocalTextIOWrapper(threading.local):
    _ori_stream: io.TextIOWrapper
    _new_stream: io.TextIOWrapper
    _cur_stream: io.TextIOWrapper

    def __init__(self, _ori_ori, _ori):
        self._ori_ori_stream = _ori_ori
        self._cur_stream = self._ori_stream = _ori
    
    def __getattr__(self, _name):
        return getattr(self._cur_stream, _name)
    
    def _set_new(self, _new):
        self._cur_stream = self._new_stream = _new

    def _to_ori(self):
        self._cur_stream = self._ori_stream

    def _to_new(self):
        self._cur_stream = self._new_stream
    
    def _clean(self):
        self._ori_stream.close()


_restored_tlocal_stdin: _TLocalTextIOWrapper = None
_restored_tlocal_stdout: _TLocalTextIOWrapper = None
_restored_tlocal_stderr: _TLocalTextIOWrapper = None


def _set_tlocal(_stream, _mode):
    if isinstance((_s_sys := getattr(sys, _stream)), _TLocalTextIOWrapper):
        return 
    
    _rs_name = f"_restored_tlocal_{_stream}"
    if isinstance((_rs_tlocal := globals()[_rs_name]), _TLocalTextIOWrapper):
        setattr(sys, _stream, _rs_tlocal)
        return

    _s_tlocal = _TLocalTextIOWrapper(_s_sys, io.TextIOWrapper(io.FileIO(os.dup(_s_sys.fileno()), mode=_mode, closefd=True), encoding='utf-8'))
    setattr(sys, _stream, _s_tlocal)
    globals()[_rs_name] = _s_tlocal


def _stdio_set_tlocal():
    _set_tlocal("stdin", "r")
    _set_tlocal("stdout", "w")
    _set_tlocal("stderr", "w")


def _stdio_unset_tlocal():
    if isinstance(sys.stdin, _TLocalTextIOWrapper):
        sys.stdin = sys.stdin._ori_ori_stream
    if isinstance(sys.stdout, _TLocalTextIOWrapper):
        sys.stdout = sys.stdout._ori_ori_stream
    if isinstance(sys.stderr, _TLocalTextIOWrapper):
        sys.stderr = sys.stderr._ori_ori_stream


def _stdio_set_new(_is, _os, _es):
    if isinstance(sys.stdin, _TLocalTextIOWrapper):
        sys.stdin._set_new(_is)
    if isinstance(sys.stdout, _TLocalTextIOWrapper):
        sys.stdout._set_new(_os)
    if isinstance(sys.stderr, _TLocalTextIOWrapper):
        sys.stderr._set_new(_es)


def _stdio_to_ori():
    if isinstance(sys.stdin, _TLocalTextIOWrapper):
        sys.stdin._to_ori()
    if isinstance(sys.stdout, _TLocalTextIOWrapper):
        sys.stdout._to_ori()
    if isinstance(sys.stderr, _TLocalTextIOWrapper):
        sys.stderr._to_ori()


def _stdio_to_new():
    if isinstance(sys.stdin, _TLocalTextIOWrapper):
        sys.stdin._to_new()
    if isinstance(sys.stdout, _TLocalTextIOWrapper):
        sys.stdout._to_new()
    if isinstance(sys.stderr, _TLocalTextIOWrapper):
        sys.stderr._to_new()


def _stdio_clean():
    if isinstance(sys.stdin, _TLocalTextIOWrapper):
        sys.stdin._clean()
    if isinstance(sys.stdout, _TLocalTextIOWrapper):
        sys.stdout._clean()
    if isinstance(sys.stderr, _TLocalTextIOWrapper):
        sys.stderr._clean()


class Pdb(pdb.Pdb, ConfigurableClass, threading.local, object):
    DefaultConfig = DefaultConfig
    config_filename = ".pdbrc.py"
    
    def __basic_init__(self, *args, **kwds):
        self.ConfigFactory = kwds.pop("Config", None)
        self.start_lineno = kwds.pop("start_lineno", None)
        self.start_filename = kwds.pop("start_filename", None)
        self.config = self.get_config(self.ConfigFactory)
        self.config.setup(self)
        if self.config.disable_pytest_capturing:
            self._disable_pytest_capture_maybe()
        kwargs = self.config.default_pdb_kwargs.copy()
        kwargs.update({**kwds, "skip": ["pdbp"]})
        super().__init__(*args, **kwargs)
        self.stderr = self.stdout
        self.prompt = self.config.prompt
        self.display_list = {}  # frame --> (name --> last seen value)
        self.sticky = self.config.sticky_by_default
        self.first_time_sticky = self.sticky
        self.ok_to_clear = False
        self.has_traceback = False
        self.sticky_ranges = {}  # frame --> (start, end)
        self.tb_lineno = {}  # frame --> lineno where the exception was raised
        self.history = []
        self.show_hidden_frames = False
        self._hidden_frames = []
        self.saved_curframe = None
        self.last_cmd = None
        self._thread_id = _thread.get_native_id()
        self._ep_counter = 0
        self._ep_path = os.path.expanduser(os.path.join(self.config.external_print_tmp_dir, str(self._thread_id)))

    def __sub_init__(self, *args, **kwds):
        parent = kwds.pop("parent", None)
        assert parent
        self.__basic_init__(*args, **kwds)
        self._ep_map = parent._ep_map

        if not os.environ.get("_PDB_DISABLE_PTY", ""):
            self.parent = parent
            self.master, self.slave = parent.master, parent.slave
            self.old_stdin, self.old_stdout, self.old_stderr = parent.old_stdin, parent.old_stdout, parent.old_stderr
            self.stdin, self.stdout, self.stderr = parent.stdin, parent.stdout, parent.stderr
            self.io_pty = parent.io_pty

        if hasattr(self, "old_stdin"):
            if os.environ.get("_PDB_W_MT", ""):
                self.stdin.readline = _rl_patch.pty_readline
            self.cmdqueue.append("_ext_pty")
    
    def __init__(self, *args, **kwds):
        self.__basic_init__(*args, **kwds)
        self._ep_map = {}

        global _atexit_registered
        if not _atexit_registered and os.getpid() == self._thread_id:
            atexit.register(self._cleanup)
            _atexit_registered = 1

        if not os.environ.get("_PDB_DISABLE_PTY", ""):
            _stdio_set_tlocal()
            master, slave = pty.openpty()
            tty.setraw(master, termios.TCSANOW)
            self.master, self.slave = master, slave

            self.old_stdin = sys.stdin._ori_stream
            self.old_stdout = sys.stdout._ori_stream
            self.old_stderr = sys.stderr._ori_stream

            self.old_stdout.write(f"Process: {os.getpid()}, Thread: {self._thread_id}, PTY: " + os.ttyname(slave) + "\n")
            self.old_stdout.flush()

            self.stdin = open(master, "r")
            self.stdout = open(os.dup(master), "w")
            self.stderr = self.stdout
            _stdio_set_new(self.stdin, self.stdout, self.stderr)

            _rl_patch.patch_hook(self.master)
            self.io_pty = True

        self.stdout = self.ensure_file_can_write_unicode(self.stdout)

        global _thread_list
        assert self._thread_id not in _thread_list
        _thread_list.append(self._thread_id)
        if hasattr(self, "old_stdin"):
            if not os.environ.get("_PDB_W_MT", ""):
                self.stdin.fileno = (lambda self: 0).__get__(self.stdin)
                self.stdout.fileno = (lambda self: 1).__get__(self.stdout)
                self.cmdqueue.append("_acquire")
            else:
                self.stdin.readline = _rl_patch.pty_readline
            self.cmdqueue.append("_ext_pty")

    def _another_tty_init(self, master):
        termios.tcsetattr(master, termios.TCSANOW, termios.tcgetattr(sys.stdin._ori_stream))
        attrs = termios.tcgetattr(master)
        attrs[3] = attrs[3] & ~termios.ECHO
        attrs[3] = attrs[3] & ~termios.ICANON
        attrs[1] = attrs[1] & ~termios.OPOST
        termios.tcsetattr(master, termios.TCSANOW, attrs)

    def _load_ext_pty(self, line):
        if hasattr(self, "_ext_stdin"):
            self._ext_stdin.close()
        if hasattr(self, "_ext_stdout"):
            self._ext_stdout.close()
        self._ext_pty = line
        with open(line, "w") as f:
            fd = f.fileno()
            self._ext_stdin = open(os.dup(fd), "r")
            self._ext_stdout = open(os.dup(fd), "w")
            self._ext_stderr = self._ext_stdout
        self.stdout.write(CLEARSCREEN)
        self.print_stack_entry(self.stack[self.curindex])

    def _istty(self, line):
        try:
            mode = os.stat(line).st_mode
            if stat.S_ISCHR(mode):
                return True
        except Exception:
            return False

    def do__ext_pty(self, arg):
        if hasattr(self, "_ext_pty"):
            return
        if hasattr(self, "parent"):
            self._ext_pty = self.parent._ext_pty
            self._ext_stdin, self._ext_stdout, self._ext_stderr = self.parent._ext_stdin, self.parent._ext_stdout, self.parent._ext_stderr
            return
        _ext_pty = input()
        assert(self._istty(_ext_pty))
        self._load_ext_pty(_ext_pty)

    def do_clean(self, arg):
        """ clean
        
        Clear the screen.
        """
        self.stdout.write(CLEARSCREEN)

    def do_EOF(self, arg):
        try:
            _readline_lock.release()
        except RuntimeError:
            pass
        return super().do_EOF(arg)
    do_EOF.__doc__ = pdb.Pdb.do_EOF.__doc__

    def do_ext_print(self, arg):
        if arg in self._ep_map:
            tmp_path = os.path.join(self._ep_path, arg)
            if not os.path.exists(tmp_path):
                print(f"Cached `{tmp_path}` is already removed. Display cancelled")
            else:
                subprocess.call(self.config.external_print_cmd.replace('<filename>', tmp_path), shell=True, **self._choose_ext_stdio())
            return
        try:
            var = eval(arg, self.curframe.f_globals, self.curframe_locals)
        except Exception:
            if not arg:
                print(
                    f"Cached external prints of <PID {self._thread_id}>:", 
                    file=self.stdout,
                )
                pprint.pp(self._ep_map, stream=self.stdout)
                return
            else:
                print(
                    'See "locals()" or "globals()" for available args!',
                    file=self.stdout,
                )
                return
        tmp_name = self.config.external_print_prefix + str(self._ep_counter) + self.config.external_print_postfix
        tmp_path = os.path.join(self._ep_path, tmp_name)
        try:
            if not os.path.exists(self._ep_path):
                os.makedirs(self._ep_path)
            with open(tmp_path, "w") as f:
                objprint.op(var, file=f)
            subprocess.call(self.config.external_print_cmd.replace('<filename>', tmp_path), shell=True, **self._choose_ext_stdio())
            print(tmp_name, file=self.stdout)
        except Exception:
            print("Invalid print!", file=self.stdout)
            return
        if self.config.external_print_cache_limit == len(self._ep_map):
            self._ep_map.pop(next(iter(self._ep_map)))
        self._ep_map[tmp_name] = codecs.escape_decode(arg)[0].decode("utf-8")
        self._ep_counter += 1
    
    do_ext_print.__doc__ = (
    """ e[xt_]p[rint] expression

    Print the value of the expression to an external file. If the expression is not given, print all the cached prints of the current thread.
    """
)
    do_ep = do_ext_print

    def do_ipython(self, arg):
        """ ipython

        Launch an IPython interactive session based on current locals().
        """
        global _ipython_enabled, _ipython_nested, _ipython_cfg
        if _ipython_enabled:
            self._launch_ipython()
        else:
            from traitlets.config.loader import Config as IPythonConfig
            from prompt_toolkit.styles import Style
            try:
                get_ipython
            except NameError:
                _ipython_nested = False
            else:
                _ipython_nested = True

            _ipython_cfg = IPythonConfig()
            _ipython_cfg.TerminalInteractiveShell.banner1 = ""
            _ipython_cfg.TerminalInteractiveShell.banner2 = ""
            _ipython_cfg.TerminalInteractiveShell.exit_msg = ""
            _ipython_cfg.TerminalInteractiveShell.highlighting_style = self.config.style
            _ipython_cfg.TerminalInteractiveShell.highlighting_style_overrides = {
                Token.Prompt: self.config.style.styles[Token.Prompt], 
                Token.PromptNum: self.config.style.styles[Token.PromptNum],
                Token.OutPrompt: self.config.style.styles[Token.OutPrompt],
                Token.OutPromptNum: self.config.style.styles[Token.OutPromptNum],
            }
            _ipython_cfg.TerminalInteractiveShell.confirm_exit = False
            _ipython_cfg.TerminalInteractiveShell.editor = self.config.ipython_editor
            _ipython_cfg.TerminalInteractiveShell.automagic = False
            _ipython_cfg.TerminalInteractiveShell.xmode = "Plain"
            _ipython_cfg.TerminalInteractiveShell.colors = "NoColor"
            _ipython_cfg._pdbp_extra_style = Style.from_dict({
                "matching-bracket.other": "#6c6c6c bg:#afaf5f",
                "matching-bracket.cursor": "#f0f0f0 bg:#afaf5f",
            })
            _ipython_enabled = True
            self._launch_ipython()

    def _launch_ipython(self):
        from IPython.terminal.embed import InteractiveShellEmbed
        from prompt_toolkit.styles import merge_styles
        self_stdout = getattr(self, "_ext_stdout", self.stdout)
        tmp_sysout, sys.stdout = sys.stdout, self_stdout
        tmp_out_fno = os.dup(1)
        tmp_in_fno = os.dup(0)
        os.dup2(self_stdout.fileno(), 1)
        os.dup2(self.stdin.fileno(), 0)
        ip_shell = InteractiveShellEmbed(config=_ipython_cfg, user_ns=self.curframe_locals)
        ip_shell.pt_app.style = merge_styles([ip_shell.style, _ipython_cfg._pdbp_extra_style])
        ip_shell()
        os.dup2(tmp_in_fno, 0)
        os.close(tmp_in_fno)
        os.dup2(tmp_out_fno, 1)
        os.close(tmp_out_fno)
        sys.stdout = tmp_sysout

    if not os.environ.get("_PDB_W_MT", ""):
        def do__acquire(self, arg):
            _readline_lock.acquire()
        
        def do_release(self, arg):
            try:
                _readline_lock.release()
            except RuntimeError:
                pass
            self.cmdqueue.append("_acquire")
            return 

        do_release.__doc__ = ( 
        """ release

        Release the readline lock, enable other threads to take control of debugging.
        """
    )
    
        def do_rcontinue(self, arg):
            self.do_release(arg)
            return self.do_continue(arg)
        
        do_rcontinue.__doc__ = ( 
        """ rc(ont(inue))

        Equivalent to executing `release` and `continue` sequentially.
        """
    )
        do_rc = do_rcont = do_rcontinue

        def do_rnext(self, arg):
            self.do_release(arg)
            return self.do_next(arg)
        
        do_rnext.__doc__ = ( 
        """ rn(ext)

        Equivalent to executing `release` and `next` sequentially.
        """
    )
        do_rn = do_rnext

        def do_rstep(self, arg):
            self.do_release(arg)
            return self.do_step(arg)
        
        do_rstep.__doc__ = ( 
        """ rs(tep)

        Equivalent to executing `release` and `step` sequentially.
        """
    )
        do_rs = do_rstep
    
    if os.environ.get("_ENABLE_PDB_RECURSIVE_TRACE", ""):

        def trace_dispatch(self, frame, event, arg):
            if r_flag := os.environ.get("_PDB_RECURSIVE_TRACE", ""):
                os.environ["_PDB_RECURSIVE_TRACE"] = ""

            with self.set_enterframe(frame):
                if self.quitting:
                    return # None
                if event == 'line':
                    return self.dispatch_line(frame) if not r_flag else sys.call_tracing(self.dispatch_line, (frame,))
                if event == 'call':
                    return self.dispatch_call(frame, arg) if not r_flag else sys.call_tracing(self.dispatch_call, (frame, arg))
                if event == 'return':
                    return self.dispatch_return(frame, arg) if not r_flag else sys.call_tracing(self.dispatch_return, (frame, arg))
                if event == 'exception':
                    return self.dispatch_exception(frame, arg) if not r_flag else sys.call_tracing(self.dispatch_exception, (frame, arg))
                if event == 'c_call':
                    return self.trace_dispatch
                if event == 'c_exception':
                    return self.trace_dispatch
                if event == 'c_return':
                    return self.trace_dispatch
                print('bdb.Bdb.dispatch: unknown debugging event:', repr(event))
                return self.trace_dispatch
    
    def _attach(self):
        assert hasattr(self, "io_pty")
        _stdio_set_tlocal()
        if not self.io_pty:
            self._exchange_stdio()

    def _detach(self):
        assert hasattr(self, "io_pty")
        if self.io_pty:
            self._exchange_stdio()

    def _choose_ext_stdio(self):
        return {
            "stdin": self.stdin if hasattr(self, "_ext_stdin") else self.old_stdin,
            "stdout": getattr(self, "_ext_stdout", self.old_stdout),
            "stderr": getattr(self, "_ext_stderr", self.old_stderr),
        }

    def _exchange_stdio(self):
        if hasattr(self, "old_stdin"):
            self.stdin, self.old_stdin = self.old_stdin, self.stdin
            self.stdout, self.old_stdout = self.old_stdout, self.stdout
            self.stderr, self.old_stderr = self.old_stderr, self.stderr
           
            self.io_pty = not self.io_pty
            if self.io_pty:
                _stdio_to_new()
                _rl_patch.patch_hook(self.master)
            else:
                _stdio_to_ori()
                _rl_patch.close_f_pty()
    
    def _cleanup(self):
        if os.path.exists(self._ep_path):
            os.system(f"rm -rf {self._ep_path}")
        
        # If Pdbp corrupt during initialization
        try:
            _pdb_lock.release()
        except RuntimeError:
            pass

        _pdb_lock.acquire()
        global _thread_list
        if self._thread_id in _thread_list:
            _thread_list.remove(self._thread_id)

        if hasattr(self, "old_stdin"):
            if _thread_list:
                _rl_patch.close_f_pty()
            else:
                _stdio_clean()
                _stdio_unset_tlocal()
                _rl_patch.unpatch_hook()

            self.stdout.flush() if self.io_pty else self.old_stdout.flush()
            time.sleep(0.1) # Ensure all output is transmitted 
            
            try:
                self._ext_stdin.close() if hasattr(self, "_ext_stdin") else None
                self._ext_stdout.close() if hasattr(self, "_ext_stdout") else None
            except OSError:
                pass

            try:
                self.stdin.close() if self.io_pty else self.old_stdin.close()
                self.stdout.close() if self.io_pty else self.old_stdout.close()
                os.close(self.slave)
            except OSError:
                pass

        _pdb_lock.release()
    
    def get_terminal_size(self):
        try:
            f_o = getattr(self, "_ext_stdout", self.stdout)
            return os.get_terminal_size(f_o.fileno())
        except Exception:
            if "linux" in sys.platform:
                return shutil.get_terminal_size((80, 20))
            try:
                return os.get_terminal_size()
            except Exception:
                return shutil.get_terminal_size((80, 20))

    def print_pdb_continue_line(self):
        width, height = self.get_terminal_size()
        pdb_continue = " PDB continue "
        border_line = ">>>>>>>>%s>>>>>>>>" % pdb_continue
        try:
            terminal_size = width
            if terminal_size < 30:
                terminal_size = 30
            border_len = terminal_size - len(pdb_continue)
            border_left_len = int(border_len / 2)
            border_right_len = int(border_len - border_left_len)
            border_left = ">" * border_left_len
            border_right = ">" * border_right_len
            border_line = (border_left + pdb_continue + border_right)
        except Exception:
            pass
        print("\n" + border_line + "\n", self.stdout)

    def _runmodule(self, module_name):
        import __main__
        import runpy
        self._wait_for_mainpyfile = True
        self._user_requested_quit = False
        mod_name, mod_spec, code = runpy._get_module_details(module_name)
        self.mainpyfile = self.canonic(code.co_filename)
        __main__.__dict__.clear()
        __main__.__dict__.update(
            {
                "__name__": "__main__",
                "__file__": self.mainpyfile,
                "__package__": mod_spec.parent,
                "__loader__": mod_spec.loader,
                "__spec__": mod_spec,
                "__builtins__": __builtins__,
            }
        )
        self.run(code)

    def _runscript(self, filename):
        import __main__
        import io
        __main__.__dict__.clear()
        __main__.__dict__.update(
            {
                "__name__": "__main__",
                "__file__": filename,
                "__builtins__": __builtins__,
            }
        )
        self._wait_for_mainpyfile = True
        self.mainpyfile = self.canonic(filename)
        self._user_requested_quit = False
        with io.open_code(filename) as fp:
            statement = (
                "exec(compile(%r, %r, 'exec'))" % (fp.read(), self.mainpyfile)
            )
        self.run(statement)

    def ensure_file_can_write_unicode(self, f):
        # Wrap with an encoder, but only if not already wrapped.
        if (not hasattr(f, "stream")
                and getattr(f, "encoding", False)
                and f.encoding.lower() != "utf-8"):
            f = codecs.getwriter("utf-8")(getattr(f, "buffer", f))
        return f

    def _disable_pytest_capture_maybe(self):
        try:
            import pytest
            import _pytest
            pytest.Config
            _pytest.config
        except (ImportError, AttributeError):
            return  # pytest is not installed
        try:
            capman = _pytest.capture.CaptureManager("global")
            capman.stop_global_capturing()
        except (KeyError, AttributeError, Exception):
            pass

    def interaction(self, frame, traceback):
        # Restore the previous signal handler at the Pdb+ prompt.
        if getattr(pdb.Pdb, "_previous_sigint_handler", None):
            try:
                signal.signal(signal.SIGINT, pdb.Pdb._previous_sigint_handler)
            except ValueError:  # ValueError: signal only works in main thread
                pass
            else:
                pdb.Pdb._previous_sigint_handler = None
        ret = None
        if not isinstance(traceback, BaseException):
            ret = self.setup(frame, traceback)
        if ret:
            self.forget()
            return
        if self.config.exec_if_unfocused:
            pass  # This option was removed!
        if (
            self.has_traceback
            and not traceback
            and self.config.exception_caught
        ):
            # The exception was caught, so no post mortem debug mode.
            self.has_traceback = False
            self.config.stack_color = self.config.regular_stack_color
            self.config.current_line_color = self.config.regular_line_color
            if self.config.post_mortem_restart:
                self.config.exception_caught = False
        if traceback or not self.sticky or self.first_time_sticky:
            if traceback:
                self.has_traceback = True
                self.config.stack_color = self.config.pm_stack_color
                self.config.current_line_color = self.config.pm_cur_line_color
            if not self.sticky:
                print(file=self.stdout)
            if not self.first_time_sticky:
                self.print_stack_entry(self.stack[self.curindex])
                self.print_hidden_frames_count()
            if self.sticky:
                if not traceback:
                    self.stdout.write(CLEARSCREEN)
            else:
                print(file=self.stdout, end="\n\033[F")
        completer = tabcompleter.setup()
        completer.config.readline.set_completer(self.complete)
        if isinstance(traceback, BaseException):
            return super().interaction(frame, traceback)
        self.config.before_interaction_hook(self)
        # Use _cmdloop on Python3, which catches KeyboardInterrupt.
        if hasattr(self, "_cmdloop"):
            self._cmdloop()
        else:
            self.cmdloop()
        self.forget()

    def print_hidden_frames_count(self):
        n = len(self._hidden_frames)
        if n and self.config.show_hidden_frames_count:
            plural = n > 1 and "s" or ""
            print(
                '   %d frame%s hidden (Use "u" and "d" to travel)'
                % (n, plural),
                file=self.stdout,
            )

    def setup(self, frame, tb):
        ret = super().setup(frame, tb)
        if not ret:
            while tb:
                lineno = lasti2lineno(tb.tb_frame.f_code, tb.tb_lasti)
                self.tb_lineno[tb.tb_frame] = lineno
                tb = tb.tb_next
        return ret

    def _is_hidden(self, frame):
        if not self.config.enable_hidden_frames:
            return False
        # Decorated code is always considered to be hidden.
        consts = frame.f_code.co_consts
        if consts and consts[-1] is _HIDE_FRAME:
            return True
        # Don't hide if this frame contains the initial set_trace.
        if frame is getattr(self, "_via_set_trace_frame", None):
            return False
        if frame.f_globals.get("__unittest"):
            return True
        if (
            frame.f_locals.get("__tracebackhide__")
            or frame.f_globals.get("__tracebackhide__")
        ):
            return True

    def get_stack(self, f, t):
        # Show all the frames except ones that should be hidden.
        fullstack, idx = super().get_stack(f, t)
        self.fullstack = fullstack
        return self.compute_stack(fullstack, idx)

    def compute_stack(self, fullstack, idx=None):
        if idx is None:
            idx = len(fullstack) - 1
        if self.show_hidden_frames:
            return fullstack, idx
        self._hidden_frames = []
        newstack = []
        for frame, lineno in fullstack:
            if self._is_hidden(frame):
                self._hidden_frames.append((frame, lineno))
            else:
                newstack.append((frame, lineno))
        newidx = idx - len(self._hidden_frames)
        return newstack, newidx

    def refresh_stack(self):
        self.stack, _ = self.compute_stack(self.fullstack)
        # Find the current frame in the new stack.
        for i, (frame, _) in enumerate(self.stack):
            if frame is self.curframe:
                self.curindex = i
                break
        else:
            self.curindex = len(self.stack) - 1
            self.curframe = self.stack[-1][0]
            self.print_current_stack_entry()

    def forget(self):
        if not hasattr(self, "lineno"):
            # Only forget if not used with recursive set_trace.
            super().forget()
        self.raise_lineno = {}

    @classmethod
    def _get_all_completions(cls, complete, text):
        r = []
        i = 0
        while True:
            comp = complete(text, i)
            if comp is None:
                break
            i += 1
            r.append(comp)
        return r

    def complete(self, text, state):
        """Handle completions from tabcompleter and the original pdb."""
        if state == 0:
            if GLOBAL_PDB:
                GLOBAL_PDB._pdbp_completing = True
            mydict = self.curframe.f_globals.copy()
            mydict.update(self.curframe.f_locals)
            completer = Completer(mydict)
            self._completions = self._get_all_completions(
                completer.complete, text
            )
            if not self._completions:
                real_pdb = super()
                for x in self._get_all_completions(real_pdb.complete, text):
                    if x not in self._completions:
                        self._completions.append(x)
            if GLOBAL_PDB:
                del GLOBAL_PDB._pdbp_completing
            # Remove "\t" from tabcompleter if there are pdb completions.
            if len(self._completions) > 1 and self._completions[0] == "\t":
                self._completions.pop(0)
        try:
            return self._completions[state]
        except IndexError:
            return None

    def _init_pygments(self):
        if not self.config.use_pygments:
            return False
        if hasattr(self, "_fmt"):
            return True
        try:
            _pdb_lock.acquire()
            # Race condition when importing
            from pygments.lexers import PythonLexer
            from pygments.formatters import TerminalFormatter
            from pygments.formatters import Terminal256Formatter
            _pdb_lock.release()
        except ImportError:
            # return False
            raise
        if hasattr(self.config, "formatter"):
            self._fmt = self.config.formatter
        else:
            if (self.config.use_terminal256formatter
                    or (self.config.use_terminal256formatter is None
                        and "256color" in os.environ.get("TERM", ""))):
                Formatter = Terminal256Formatter
            else:
                Formatter = TerminalFormatter
            self._fmt = Formatter(bg=self.config.bg,
                                  colorscheme=self.config.colorscheme,
                                  style=self.config.style)
        self._lexer = PythonLexer()
        return True

    stack_entry_regexp = re.compile(r"(.*?)\(([0-9]+?)\)(.*)", re.DOTALL)

    def format_stack_entry(self, frame_lineno, lprefix=": "):
        entry = super().format_stack_entry(frame_lineno, lprefix)
        entry = self.try_to_decode(entry)
        if self.config.highlight:
            match = self.stack_entry_regexp.match(entry)
            if match:
                filename, lineno, other = match.groups()
                other = self.format_source(other.rstrip()).rstrip()
                filename = Color.set(self.config.filename_color, filename)
                lineno = Color.set(self.config.line_number_color, lineno)
                entry = "%s(%s)%s" % (filename, lineno, other)
        return entry

    def try_to_decode(self, s):
        for encoding in self.config.encodings:
            try:
                return s.decode(encoding)
            except (UnicodeDecodeError, AttributeError):
                pass
        return s

    def format_source(self, src, *, return_str_code=False):
        if not self._init_pygments():
            if return_str_code:
                return src, None
            else:
                return src
        from pygments import highlight
        src = self.try_to_decode(src)
        rt = highlight(src, self._lexer, self._fmt)
        if return_str_code:
            str_code = re.match(r".*(38;5;\d+)manystr.*", highlight("'anystr'", self._lexer, self._fmt)).group(1)
            return rt, str_code
        else:
            return rt 

    def format_line(self, lineno, marker, line):
        lineno = "%4d" % lineno
        if self.config.highlight:
            lineno = Color.set(self.config.line_number_color, lineno)
        line = "%s  %2s %s" % (lineno, marker, line)
        if self.config.highlight and marker == "->":
            if self.config.current_line_color:
                line = setbgcolor(line, self.config.current_line_color)
        elif self.config.highlight and marker == ">>":
            if self.config.exc_line_color:
                line = setbgcolor(line, self.config.exc_line_color)
        return line

    def parseline(self, line):
        if line.startswith("!!"):
            line = line[2:]
            return super().parseline(line)
        cmd, arg, newline = super().parseline(line)
        if arg and arg.endswith("?"):
            if hasattr(self, "do_" + cmd):
                cmd, arg = ("help", cmd)
            elif arg.endswith("??"):
                arg = cmd + arg.split("?")[0]
                cmd = "source"
                self.do_inspect(arg)
                self.stdout.write("%-28s\n" % Color.set(Color.red, "Source:"))
            else:
                arg = cmd + arg.split("?")[0]
                cmd = "inspect"
                return cmd, arg, newline
        if (
            cmd == "f"
            and len(newline) > 1
            and (newline[1] == "'" or newline[1] == '"')
        ):
            return super().parseline("!" + line)

        if (
            cmd
            and hasattr(self, "do_" + cmd)
            and (
                cmd in self.curframe.f_globals
                or cmd in self.curframe.f_locals
                or arg.startswith("=")
            )
        ):
            return super().parseline("!" + line)

        if cmd == "list" and arg.startswith("("):
            line = "!" + line
            return super().parseline(line)

        return cmd, arg, newline

    def do_inspect(self, arg):
        if not arg:
            print('Inspect Usage: "inspect <VAR>"', file=self.stdout)
            print(
                "Local variables: %r" % self.curframe_locals.keys(),
                file=self.stdout,
            )
            return
        try:
            obj = self._getval(arg)
        except Exception:
            print(
                'See "locals()" or "globals()" for available args!',
                file=self.stdout,
            )
            return
        data = OrderedDict()
        data["Type"] = type(obj).__name__
        data["String Form"] = str(obj).strip()
        try:
            data["Length"] = len(obj)
        except TypeError:
            pass
        try:
            data["File"] = inspect.getabsfile(obj)
        except TypeError:
            pass
        if (
            isinstance(obj, type)
            and hasattr(obj, "__init__")
            and getattr(obj, "__module__") != "__builtin__"
        ):
            data["Docstring"] = obj.__doc__
            data["Constructor information"] = ""
            try:
                data[" Definition"] = "%s%s" % (arg, signature(obj))
            except ValueError:
                pass
            data[" Docstring"] = obj.__init__.__doc__
        else:
            try:
                data["Definition"] = "%s%s" % (arg, signature(obj))
            except (TypeError, ValueError):
                pass
            data["Docstring"] = obj.__doc__
        for key, value in data.items():
            formatted_key = Color.set(Color.red, key + ":")
            self.stdout.write("%-28s %s\n" % (formatted_key, value))

    def default(self, line):
        if self._istty(line):
            self._load_ext_pty(line)
            return
        self.history.append(line)
        return super().default(line)

    def do_help(self, arg):
        try:
            return super().do_help(arg)
        except AttributeError:
            print("*** No help for '{command}'".format(command=arg),
                  file=self.stdout)
    do_help.__doc__ = pdb.Pdb.do_help.__doc__

    def help_hidden_frames(self):
        print('Use "u" and "d" to travel up/down the stack.', file=self.stdout)

    def do_longlist(self, arg):
        self.last_cmd = self.lastcmd = "longlist"
        self.sticky = True
        self._print_if_sticky()
    do_ll = do_longlist

    def do_jump(self, arg):
        self.last_cmd = self.lastcmd = "jump"
        if self.curindex + 1 != len(self.stack):
            self.error("You can only jump within the bottom frame!")
            return
        try:
            arg = int(arg)
        except ValueError:
            self.error("The 'jump' command requires a line number!")
        else:
            try:
                self.curframe.f_lineno = arg
                self.stack[self.curindex] = self.stack[self.curindex][0], arg
                self.print_current_stack_entry()
            except ValueError as e:
                self.error('Jump failed: %s' % e)
    do_j = do_jump

    def _printlonglist(self, linerange=None, fnln=None, nc_fnln=""):
        try:
            if self.curframe.f_code.co_name == "<module>":
                lines, _ = inspect.findsource(self.curframe)
                lineno = 1
            else:
                try:
                    lines, lineno = inspect.getsourcelines(self.curframe)
                except Exception:
                    print(file=self.stdout)
                    self.sticky = False
                    self.print_stack_entry(self.stack[self.curindex])
                    self.sticky = True
                    print(file=self.stdout, end="\n\033[F")
                    return
        except IOError as e:
            try:
                self.sticky = False
                self.print_stack_entry(self.stack[self.curindex])
                self.sticky = True
                return
            except Exception:
                self.sticky = True
                print("** (%s) **" % e, file=self.stdout)
                return
        if linerange:
            start, end = linerange
            start = max(start, lineno)
            end = min(end, lineno + len(lines))
            lines = lines[start - lineno:end - lineno]
            lineno = start
        self._print_lines_pdbp(lines, lineno, fnln=fnln, nc_fnln=nc_fnln)

    def _print_lines_pdbp(
        self, lines, lineno, print_markers=True, fnln=None, nc_fnln=""
    ):
        dots = "..."
        offset = 0
        try:
            lineno_int = int(lineno)
        except Exception:
            lineno = 1
            lineno_int = 1
        if lineno_int == 1:
            dots = ""
        elif lineno_int > 99999:
            dots = "......"
        elif lineno_int > 9999:
            dots = "....."
        elif lineno_int > 999:
            dots = "...."
        elif lineno_int > 99:
            dots = " ..."
        elif lineno_int > 9:
            dots = "  .."
        else:
            dots = "   ."
        max_line = int(lineno) + len(lines) - 1
        if max_line > 9999:
            offset = 1
        if max_line > 99999:
            offset = 2
        exc_lineno = self.tb_lineno.get(self.curframe, None)
        lines = [line.replace("\t", "    ")
                 for line in lines]  # force tabs to 4 spaces
        lines = [line.rstrip() for line in lines]
        width, height = self.get_terminal_size()
        width = width - offset
        height = height - 1
        overflow = 0
        height_counter = height
        if not self.config.truncate_long_lines:
            for line in lines:
                if len(line) > width - 9:
                    overflow += 1
                height_counter -= 1
                if height_counter <= 0:
                    break
        if self.config.truncate_long_lines:
            maxlength = max(width - 9, 16)
            lines = [set_line_width(line, maxlength) for line in lines]
        else:
            maxlength = max(map(get_width, lines))
        if self.config.highlight:
            # Fill line with spaces. This is important when a bg color is
            # is used for highlighting the current line (via setbgcolor).
            tll = self.config.truncate_long_lines
            lines = [set_line_width(line, maxlength, tll) for line in lines]
            src = self.format_source("\n".join(lines))
            lines = src.splitlines()
        if height >= 6:
            last_marker_line = max(
                self.curframe.f_lineno,
                exc_lineno if exc_lineno else 0
            ) - lineno
            if last_marker_line >= 0:
                more_overflow = int(len(nc_fnln) / width)
                overflow = overflow + more_overflow
                maxlines = last_marker_line + (height * 2 // 3)
                maxlines = maxlines - math.ceil(overflow * 1 / 3)
                if len(lines) > maxlines:
                    lines = lines[:maxlines]
                    lines.append(Color.set("39;49;1", "..."))
        self.config.exception_caught = False
        for i, line in enumerate(lines):
            marker = ""
            if lineno == self.curframe.f_lineno and print_markers:
                marker = "->"
            elif lineno == exc_lineno and print_markers:
                marker = ">>"
                self.config.exception_caught = True
            lines[i] = self.format_line(lineno, marker, line)
            lineno += 1
        if self.ok_to_clear:
            self.stdout.write(CLEARSCREEN)
        if fnln:
            print(fnln, file=self.stdout)
            if int(lineno) > 1:
                num_color = self.config.line_number_color
                print(Color.set(num_color, dots), file=self.stdout)
            else:
                print(file=self.stdout)
        print("\n".join(lines), file=self.stdout, end="\n\n\033[F")

    def do_list(self, arg):
        try:
            import linecache
            y = 0
            if run_from_main:
                y = 6
            filename = self.curframe.f_code.co_filename
            lines = linecache.getlines(filename, self.curframe.f_globals)
            if (
                not arg
                and (
                    (self.last_cmd == "list" and self.lineno >= len(lines) + y)
                    or self.last_cmd != "list"
                    or (
                        self.saved_curframe != self.curframe
                        or self.lineno < self.curframe.f_lineno
                    )
                )
            ):
                arg = "."  # Go back to the active cursor point
        except Exception:
            pass
        self.last_cmd = self.lastcmd = "list"
        self.saved_curframe = self.curframe
        oldstdout = self.stdout
        self.stdout = StringIO()
        super().do_list(arg)
        src, str_code = self.format_source(self.stdout.getvalue(), return_str_code=True)
        if str_code is not None and str_code != self.config.line_number_color:
            def _re_lineno_helper(ma):
                gp1 = ma.group(1) or ma.group(3)
                gp2 = ma.group(2) or ma.group(4)
                gp3 = ma.group(5)
                if not gp3:
                    return f"{gp1}{self.config.line_number_color}{gp2}"
                else:
                    return f"{gp1}{self.config.line_number_color}{gp2}\x1b[39m\x1b[{str_code}m{gp3}"
            src = re.sub(rf"(\x1b\[38;5;15m[ ]+\x1b\[39m\x1b\[)?{str_code}(m\d+\x1b\[39m.*\n)|(\x1b\[){str_code}(m[ ]*\d+)(.*\x1b\[39m\n)", _re_lineno_helper, src)
        self.stdout = oldstdout
        print(src, file=self.stdout, end="\n\033[F")

    do_list.__doc__ = pdb.Pdb.do_list.__doc__
    do_l = do_list
    
    def do_continue(self, arg):
        self.last_cmd = self.lastcmd = "continue"
        if arg != "":
            self.do_tbreak(arg)
        return super().do_continue(arg)
    do_continue.__doc__ = pdb.Pdb.do_continue.__doc__
    do_c = do_cont = do_continue

    def do_next(self, arg):
        self.last_cmd = self.lastcmd = "next"
        return super().do_next(arg)
    do_next.__doc__ = pdb.Pdb.do_next.__doc__
    do_n = do_next

    def do_step(self, arg):
        self.last_cmd = self.lastcmd = "step"
        return super().do_step(arg)
    do_step.__doc__ = pdb.Pdb.do_step.__doc__
    do_s = do_step

    def do_until(self, arg):
        self.last_cmd = self.lastcmd = "until"
        return super().do_until(arg)
    do_until.__doc__ = pdb.Pdb.do_until.__doc__
    do_unt = do_until

    def do_p(self, arg):
        try:
            self.message(repr(self._getval(arg)))
        except Exception:
            if not arg:
                print('Print usage: "p <VAR>"', file=self.stdout)
                print(
                    "Local variables: %r" % self.curframe_locals.keys(),
                    file=self.stdout,
                )
                return
            else:
                print(
                    'See "locals()" or "globals()" for available args!',
                    file=self.stdout,
                )
                return
    do_p.__doc__ = pdb.Pdb.do_p.__doc__

    def do_pp(self, arg):
        width, _ = self.get_terminal_size()
        try:
            pprint.pprint(self._getval(arg), self.stdout, width=width)
        except Exception:
            if not arg:
                print('PrettyPrint usage: "pp <VAR>"', file=self.stdout)
                print(
                    "Local variables: %r" % self.curframe_locals.keys(),
                    file=self.stdout,
                )
                return
            else:
                print(
                    'See "locals()" or "globals()" for available args!',
                    file=self.stdout,
                )
                return
    do_pp.__doc__ = pdb.Pdb.do_pp.__doc__

    def do_debug(self, arg):
        self.last_cmd = self.lastcmd = "debug"
        Config = self.ConfigFactory
        ori_globals = getattr(self, "_ori_globals", globals())

        class PdbpWithConfig(self.__class__):
            def __init__(self_withcfg, *args, **kwargs):
                kwargs.setdefault("Config", Config)
                kwargs["parent"] = self
                super(PdbpWithConfig, self_withcfg).__sub_init__(*args, **kwargs)
                self_withcfg.use_rawinput = self.use_rawinput
                self_withcfg.config.external_print_prefix = self.config.external_print_prefix + self_withcfg.config.external_print_subfix
                self_withcfg._ori_globals = ori_globals
                ori_globals["GLOBAL_PDB"] = self_withcfg
        
        for cls_ in self.__class__.__mro__[1:]:
            do_debug_func = getattr(cls_, "do_debug", None)
            if not do_debug_func:
                continue
            if do_debug_func.__module__ == "pdb":
                break
            else:
                do_debug_func = None

        assert(do_debug_func)
        newglobals = do_debug_func.__globals__.copy()
        newglobals["Pdb"] = PdbpWithConfig
        orig_do_debug = rebind_globals(do_debug_func, newglobals)
        try:
            rt = orig_do_debug(self, arg)
        except Exception:
            exc_info = sys.exc_info()[:2]
            msg = traceback.format_exception_only(*exc_info)[-1].strip()
            self.error(msg)
        finally:
            ori_globals["GLOBAL_PDB"] = self
        return rt

    do_debug.__doc__ = pdb.Pdb.do_debug.__doc__

    def do_run(self, arg):
        """Restart/Rerun during ``python -m pdbp <script.py>`` mode."""
        self.last_cmd = self.lastcmd = "run"
        if arg:
            import shlex
            argv0 = sys.argv[0:1]
            sys.argv = shlex.split(arg)
            sys.argv[:0] = argv0
        raise Restart
    do_restart = do_run

    def do_interact(self, arg):
        ns = self.curframe.f_globals.copy()
        ns.update(self.curframe.f_locals)
        code.interact("*interactive*", local=ns)

    def do_track(self, arg):
        try:
            from rpython.translator.tool.reftracker import track
        except ImportError:
            print(
                "** cannot import pypy.translator.tool.reftracker **",
                file=self.stdout,
            )
            print(
                "This command requires pypy to be in the current PYTHONPATH.",
                file=self.stdout,
            )
            return
        try:
            val = self._getval(arg)
        except Exception:
            pass
        else:
            track(val)

    def _get_display_list(self):
        return self.display_list.setdefault(self.curframe, {})

    def _getval_or_undefined(self, arg):
        try:
            return eval(arg, self.curframe.f_globals,
                        self.curframe.f_locals)
        except NameError:
            return undefined

    def do_display(self, arg):
        try:
            value = self._getval_or_undefined(arg)
        except Exception:
            return
        self._get_display_list()[arg] = value

    def do_undisplay(self, arg):
        try:
            del self._get_display_list()[arg]
        except KeyError:
            print("** %s not in the display list **" % arg, file=self.stdout)

    def __get_return_color(self, s):
        frame, lineno = self.stack[self.curindex]
        if self.has_traceback or "__exception__" in frame.f_locals:
            self.config.last_return_color = self.config.pm_return_value_color
            return self.config.last_return_color
        the_return_color = None
        return_value = s.strip().split("return ")[-1]
        if return_value == "None":
            the_return_color = self.config.none_return_value_color
        elif return_value == "True":
            the_return_color = self.config.true_return_value_color
        elif return_value in ["False", "", "[]", r"{}"]:
            the_return_color = self.config.false_return_value_color
        elif len(return_value) > 0 and return_value[0].isdecimal():
            the_return_color = self.config.num_return_value_color
        else:
            the_return_color = self.config.return_value_color
        self.config.last_return_color = the_return_color
        return self.config.last_return_color

    def _print_if_sticky(self):
        if self.sticky:
            if self.first_time_sticky:
                self.first_time_sticky = False
            self.ok_to_clear = True
            frame, lineno = self.stack[self.curindex]
            filename = self.canonic(frame.f_code.co_filename)
            lno = Color.set(self.config.line_number_color, "%r" % lineno)
            short_filename = filename
            if self.config.shorten_path:
                try:
                    home_dir = os.path.expanduser("~")
                    if (
                        len(home_dir) > 4
                        and filename.startswith(home_dir)
                        and filename.count(home_dir) == 1
                    ):
                        short_filename = filename.replace(home_dir, "~")
                except Exception:
                    pass
            fname = Color.set(self.config.filename_color, short_filename)
            fnln = None
            if not self.curindex:
                self.curindex = 0
            colored_index = Color.set(self.config.stack_color, self.curindex)
            fnln = "[%s] > %s(%s)" % (colored_index, fname, lno)
            nc_fnln = "[%s] > %s(%s)" % (self.curindex, filename, lineno)
            sticky_range = self.sticky_ranges.get(self.curframe, None)
            self._printlonglist(sticky_range, fnln=fnln, nc_fnln=nc_fnln)
            needs_extra_line = False
            if "__exception__" in frame.f_locals:
                s = self._format_exc_for_sticky(
                    frame.f_locals["__exception__"]
                )
                if s:
                    last_return_color = self.config.last_return_color
                    if (
                        last_return_color == self.config.pm_return_value_color
                        and not self.config.exception_caught
                    ):
                        print(s, file=self.stdout)
                        needs_extra_line = True
            elif "exc" in frame.f_locals and "msg" in frame.f_locals:
                s = str(frame.f_locals["msg"]).strip()
                e = str(frame.f_locals["exc"]).strip()
                e = e.split("<class '")[-1].split("'>")[0] + ":"
                if s and self.has_traceback:
                    if self.config.highlight:
                        the_return_color = self.__get_return_color(s)
                        s = Color.set(the_return_color, s)
                        e = Color.set(the_return_color, e)
                    last_return_color = self.config.last_return_color
                    lastline = None
                    try:
                        lastline = inspect.getsourcelines(self.curframe)[0][-1]
                        lastline = str(lastline)
                    except Exception:
                        lastline = ""
                    if (
                        last_return_color == self.config.pm_return_value_color
                        and not self.config.exception_caught
                        and "raise " in lastline
                        and "(msg" in lastline.replace(" ", "")
                    ):
                        print(e, file=self.stdout)
                        print(" " + s, file=self.stdout)
                        needs_extra_line = True
            elif "msg" in frame.f_locals or "message" in frame.f_locals:
                s = None
                s2 = None
                if "msg" in frame.f_locals:
                    s = str(frame.f_locals["msg"]).strip()
                if "message" in frame.f_locals:
                    s2 = str(frame.f_locals["message"]).strip()
                if (s or s2) and self.has_traceback:
                    if self.config.highlight:
                        if s:
                            the_return_color = self.__get_return_color(s)
                            s = Color.set(the_return_color, s)
                        if s2:
                            the_return_color_2 = self.__get_return_color(s2)
                            s2 = Color.set(the_return_color_2, s2)
                    last_return_color = self.config.last_return_color
                    lastline = None
                    try:
                        lastline = inspect.getsourcelines(self.curframe)[0][-1]
                        lastline = str(lastline)
                    except Exception:
                        lastline = ""
                    if (
                        last_return_color == self.config.pm_return_value_color
                        and not self.config.exception_caught
                        and "raise " in lastline
                        and s
                        and "(msg" in lastline.replace(" ", "")
                    ):
                        print(s, file=self.stdout)
                        needs_extra_line = True
                    elif (
                        last_return_color == self.config.pm_return_value_color
                        and not self.config.exception_caught
                        and "raise " in lastline
                        and s2
                        and "(message" in lastline.replace(" ", "")
                    ):
                        print(s2, file=self.stdout)
                        needs_extra_line = True
            if "__return__" in frame.f_locals:
                rv = frame.f_locals["__return__"]
                try:
                    s = repr(rv)
                except KeyboardInterrupt:
                    raise
                except Exception:
                    s = "(unprintable return value)"
                s = " return " + s
                if self.config.highlight:
                    if (
                        needs_extra_line
                        and frame.f_locals["__return__"] is None
                    ):
                        # There was an Exception. And returning None.
                        the_return_color = self.config.exc_line_color
                        s = s + " "
                    else:
                        the_return_color = self.__get_return_color(s)
                    s = Color.set(the_return_color, s)
                print(s, file=self.stdout)
                needs_extra_line = True
            if needs_extra_line:
                print(file=self.stdout, end="\n\033[F")

    def _format_exc_for_sticky(self, exc):
        if len(exc) != 2:
            return "pdbp: got unexpected __exception__: %r" % (exc,)
        exc_type, exc_value = exc
        s = ""
        try:
            try:
                try:
                    module = str(exc_type.__module__)
                    module = module.split("<class '")[-1].split("'>")[0]
                    if module != "builtins":
                        s = module + "." + exc_type.__name__.strip()
                    else:
                        s = exc_type.__name__.strip()
                except Exception:
                    s = exc_type.__name__.strip()
            except AttributeError:
                s = str(exc_type).strip()
            if exc_value is not None:
                s += ": "
                s2 = str(exc_value)
                if s2.startswith("Message:") and s2.count("Message:") == 1:
                    s2 = "\n " + s2.split("Message:")[-1].strip()
                s += s2
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            try:
                s += "(unprintable exception: %r)" % (exc,)
            except Exception:
                s += "(unprintable exception)"
        if self.config.highlight:
            the_return_color = self.__get_return_color(s)
            s = Color.set(the_return_color, s)
        return s

    def do_sticky(self, arg):
        """Toggle sticky mode. Usage: sticky [start end]"""
        if arg:
            try:
                start, end = map(int, arg.split())
            except ValueError:
                print("** Error when parsing argument: %s **" % arg,
                      file=self.stdout)
                return
            self.sticky = True
            self.sticky_ranges[self.curframe] = start, end + 1
        else:
            self.sticky = not self.sticky
            self.sticky_range = None
        if self.sticky:
            self._print_if_sticky()
        else:
            print(file=self.stdout)
            self.print_stack_entry(self.stack[self.curindex])
            print(file=self.stdout, end="\n\033[F")

    def do_truncate(self, arg):
        # Toggle line truncation. Usage: "truncate" / "trun".
        # (Changes only appear when "sticky" mode is active.)
        # When enabled, all lines take on the screen width.
        self.config.truncate_long_lines = not self.config.truncate_long_lines
        self.print_current_stack_entry()
    do_trun = do_truncate

    def print_stack_trace(self):
        try:
            for frame_index, frame_lineno in enumerate(self.stack):
                self.print_stack_entry(frame_lineno, frame_index=frame_index)
        except KeyboardInterrupt:
            pass

    def print_stack_entry(
        self, frame_lineno, prompt_prefix=pdb.line_prefix, frame_index=None
    ):
        if self.sticky:
            return
        frame_index = frame_index if frame_index is not None else self.curindex
        frame, lineno = frame_lineno
        colored_index = Color.set(self.config.stack_color, frame_index)
        if frame is self.curframe:
            indicator = " >"
            color = self.config.regular_line_color
            if self.has_traceback:
                color = self.config.exc_line_color
                if frame_index == len(self.stack) - 1:
                    color = self.config.pm_cur_line_color
            ind = setbgcolor(indicator, color)
            print("[%s]%s" % (colored_index, ind), file=self.stdout, end=" ")
        else:
            print("[%s]  " % colored_index, file=self.stdout, end=" ")
        stack_entry = self.format_stack_entry(frame_lineno, prompt_prefix)
        print(stack_entry, file=self.stdout)
        if not self.sticky:
            print(file=self.stdout, end="\n\033[F")
            if (
                "\n-> except " in stack_entry or "\n-> except:" in stack_entry
            ):
                self.config.exception_caught = True

    def print_current_stack_entry(self):
        if self.sticky:
            self._print_if_sticky()
        else:
            print(file=self.stdout)
            self.print_stack_entry(self.stack[self.curindex])
            print(file=self.stdout, end="\n\033[F")

    def preloop(self):
        self._print_if_sticky()
        display_list = self._get_display_list()
        for expr, oldvalue in display_list.items():
            newvalue = self._getval_or_undefined(expr)
            if newvalue is not oldvalue or newvalue != oldvalue:
                display_list[expr] = newvalue
                print("%s: %r --> %r" % (expr, oldvalue, newvalue),
                      file=self.stdout)

    def _get_position_of_arg(self, arg):
        try:
            obj = self._getval(arg)
        except Exception:
            return None, None, None
        if isinstance(obj, str):
            return obj, 1, None
        try:
            filename = inspect.getabsfile(obj)
            lines, lineno = inspect.getsourcelines(obj)
        except (IOError, TypeError) as e:
            print("** Error: %s **" % e, file=self.stdout)
            return None, None, None
        return filename, lineno, lines

    def do_source(self, arg):
        _, lineno, lines = self._get_position_of_arg(arg)
        if lineno is None:
            return
        try:
            frame = self.curframe
            filename = self.canonic(frame.f_code.co_filename)
            nc_fnln = "[%s] > %s(%s)" % (self.curindex, filename, lineno)
            self._print_lines_pdbp(
                lines, lineno, print_markers=False, nc_fnln=nc_fnln
            )
        except Exception:
            self._print_lines_pdbp(lines, lineno, print_markers=False)

    def do_frame(self, arg):
        try:
            arg = int(arg)
        except (ValueError, TypeError):
            print(
                '*** Expected a number, got "{0}"'.format(arg),
                file=self.stdout
            )
            return
        if arg < 0 or arg >= len(self.stack):
            print("*** Out of range", file=self.stdout)
        else:
            self.curindex = arg
            self.curframe = self.stack[self.curindex][0]
            self.curframe_locals = self.curframe.f_locals
            self.print_current_stack_entry()
            self.lineno = None
    do_f = do_frame

    def do_up(self, arg="1"):
        self.last_cmd = self.lastcmd = "up"
        arg = "1" if arg == "" else arg
        try:
            arg = int(arg)
        except (ValueError, TypeError):
            print(
                '*** Expected a number, got "{0}"'.format(arg),
                file=self.stdout
            )
            return
        if self.curindex - arg < 0:
            print("*** Oldest frame", file=self.stdout)
        else:
            self.curindex = self.curindex - arg
            self.curframe = self.stack[self.curindex][0]
            self.curframe_locals = self.curframe.f_locals
            self.print_current_stack_entry()
            self.lineno = None
    do_up.__doc__ = pdb.Pdb.do_up.__doc__
    do_u = do_up

    def do_down(self, arg="1"):
        self.last_cmd = self.lastcmd = "down"
        arg = "1" if arg == "" else arg
        try:
            arg = int(arg)
        except (ValueError, TypeError):
            print(
                '*** Expected a number, got "{0}"'.format(arg),
                file=self.stdout
            )
            return
        if self.curindex + arg >= len(self.stack):
            print("*** Newest frame", file=self.stdout)
        else:
            self.curindex = self.curindex + arg
            self.curframe = self.stack[self.curindex][0]
            self.curframe_locals = self.curframe.f_locals
            self.print_current_stack_entry()
            self.lineno = None
    do_down.__doc__ = pdb.Pdb.do_down.__doc__
    do_d = do_down

    def do_where(self, arg):
        self.last_cmd = self.lastcmd = "where"
        self.sticky = False
        print(file=self.stdout)
        self.print_stack_trace()
    do_w = do_where
    do_bt = do_where

    def _open_editor(self, editor, lineno, filename):
        filename = filename.replace('"', '\\"')
        subprocess.call(editor.replace('<filename>', filename).replace('<lineno>', str(lineno)), shell=True, **self._choose_ext_stdio())

    def _get_current_position(self):
        frame = self.curframe
        lineno = frame.f_lineno
        filename = os.path.abspath(frame.f_code.co_filename)
        return filename, lineno

    def do_edit(self, arg):
        "Open an editor visiting the current file at the current line"
        if arg == "":
            filename, lineno = self._get_current_position()
        else:
            filename, lineno, _ = self._get_position_of_arg(arg)
            if filename is None:
                return
        match = re.match(r".*<\d+-codegen (.*):(\d+)>", filename)
        if match:
            filename = match.group(1)
            lineno = int(match.group(2))
        editor = self.config.editor
        self._open_editor(editor, lineno, filename)

    def _get_history(self):
        return [s for s in self.history if not side_effects_free.match(s)]

    def _get_history_text(self):
        import linecache
        line = linecache.getline(self.start_filename, self.start_lineno)
        nspaces = len(line) - len(line.lstrip())
        indent = " " * nspaces
        history = [indent + s for s in self._get_history()]
        return "\n".join(history) + "\n"

    def set_trace(self, frame=None):
        """Remember starting frame. Used with pytest."""
        if frame is None:
            frame = sys._getframe().f_back
        self._via_set_trace_frame = frame
        return super().set_trace(frame)

    def is_skipped_module(self, module_name):
        if module_name is None:
            return False
        return super().is_skipped_module(module_name)

    def error(self, msg):
        """Override/enhance default error method to display tracebacks."""
        print("***", msg, file=self.stdout)

        if not self.config.show_traceback_on_error:
            return

        etype, evalue, tb = sys.exc_info()
        if tb and tb.tb_frame.f_code.co_name == "default":
            tb = tb.tb_next
            if tb and tb.tb_frame.f_code.co_filename == "<stdin>":
                tb = tb.tb_next
                if tb:
                    self._remove_bdb_context(evalue)
                    tb_limit = self.config.show_traceback_on_error_limit
                    fmt_exc = traceback.format_exception(
                        etype, evalue, tb, limit=tb_limit
                    )
                    # Remove the last line (exception string again).
                    if len(fmt_exc) > 1 and fmt_exc[-1][0] != " ":
                        fmt_exc.pop()
                    print("".join(fmt_exc).rstrip(), file=self.stdout)

    @staticmethod
    def _remove_bdb_context(evalue):
        removed_bdb_context = evalue
        while removed_bdb_context.__context__:
            ctx = removed_bdb_context.__context__
            if (
                isinstance(ctx, AttributeError)
                and ctx.__traceback__.tb_frame.f_code.co_name == "onecmd"
            ):
                removed_bdb_context.__context__ = None
                break
            removed_bdb_context = removed_bdb_context.__context__


if hasattr(pdb, "_usage"):
    _usage = pdb._usage

# Copy some functions from pdb.py, but rebind the global dictionary.
for name in "run runeval runctx runcall pm main".split():
    func = getattr(pdb, name)
    globals()[name] = rebind_globals(func, globals())
del name, func


def post_mortem(t=None, Pdb=Pdb):
    if t is None:
        t = sys.exc_info()[2]
        assert t is not None, "post_mortem outside of exception context"
    p = Pdb()
    p.reset()
    p.interaction(None, t)


GLOBAL_PDB = None


def set_trace(frame=None, header=None, Pdb=Pdb, **kwds):
    _pdb_lock.acquire()
    global GLOBAL_PDB
    if GLOBAL_PDB and hasattr(GLOBAL_PDB, "_pdbp_completing"):
        _pdb_lock.release()
        return
    if GLOBAL_PDB and hasattr(GLOBAL_PDB, "old_stdin"):
        GLOBAL_PDB._attach()
    if frame is None:
        frame = sys._getframe().f_back
    if GLOBAL_PDB:
        pdb = GLOBAL_PDB
        sys.settrace(None)
    else:
        filename = frame.f_code.co_filename
        lineno = frame.f_lineno
        pdb = Pdb(start_lineno=lineno, start_filename=filename, **kwds)
        GLOBAL_PDB = pdb
    if header is not None:
        pdb.message(header)
    pdb.set_trace(frame)
    _pdb_lock.release()


def cleanup():
    global GLOBAL_PDB
    GLOBAL_PDB = None


def set_none(restore_stdio=True):
    _pdb_lock.acquire()
    sys.settrace(None)
    global GLOBAL_PDB
    if restore_stdio and GLOBAL_PDB and hasattr(GLOBAL_PDB, "old_stdin"):
        GLOBAL_PDB._detach()
    _pdb_lock.release()


def xpm(Pdb=Pdb):
    """
    Enter a post-mortem pdb related to the exception just catched.
    (Used inside an except clause.)
    """
    info = sys.exc_info()
    print(traceback.format_exc())
    post_mortem(info[2], Pdb)


def enable():
    global set_trace
    set_trace = enable.set_trace


enable.set_trace = set_trace


def disable():
    global set_trace
    set_trace = disable.set_trace


disable.set_trace = lambda frame=None, Pdb=Pdb: None


def set_tracex():
    print("PDB!")


set_tracex._dont_inline_ = True

_HIDE_FRAME = object()


def hideframe(func):
    c = func.__code__
    new_co_consts = c.co_consts + (_HIDE_FRAME,)
    if hasattr(c, "replace"):
        c = c.replace(co_consts=new_co_consts)
    else:
        c = types.CodeType(
            c.co_argcount, c.co_kwonlyargcount, c.co_nlocals, c.co_stacksize,
            c.co_flags, c.co_code,
            c.co_consts + (_HIDE_FRAME,),
            c.co_names, c.co_varnames, c.co_filename,
            c.co_name, c.co_firstlineno, c.co_lnotab,
            c.co_freevars, c.co_cellvars,
        )
    func.__code__ = c
    return func


def always(obj, value):
    return True


def break_on_setattr(attrname, condition=always, Pdb=Pdb):
    def decorator(cls):
        old___setattr__ = cls.__setattr__

        @hideframe
        def __setattr__(self, attr, value):
            if attr == attrname and condition(self, value):
                frame = sys._getframe().f_back
                pdb_ = Pdb()
                pdb_.set_trace(frame)
                pdb_.stopframe = frame
                pdb_.interaction(frame, None)
            old___setattr__(self, attr, value)
        cls.__setattr__ = __setattr__
        return cls
    return decorator


import pdb  # noqa
pdb.Pdb = Pdb
pdb.Color = Color
pdb.DefaultConfig = DefaultConfig
pdb.OrderedDict = OrderedDict
pdb.Completer = Completer
pdb.CLEARSCREEN = CLEARSCREEN
pdb.GLOBAL_PDB = GLOBAL_PDB
pdb.ConfigurableClass = ConfigurableClass
pdb.side_effects_free = side_effects_free
pdb.rebind_globals = rebind_globals
pdb.lasti2lineno = lasti2lineno
pdb.tabcompleter = tabcompleter
pdb.post_mortem = post_mortem
pdb.set_tracex = set_tracex
pdb.setbgcolor = setbgcolor
pdb.set_trace = set_trace
pdb.signature = signature
pdb.Undefined = Undefined
pdb.cleanup = cleanup
pdb.xpm = xpm
pdb.set_none = set_none
pdb.inject_debug = inject_debug
pdb.remove_debug = remove_debug
pdb.show_debug = show_debug


def main():
    import getopt
    opts, args = getopt.getopt(sys.argv[1:], "mhc:", ["help", "command="])
    if not args:
        print(_usage)
        sys.exit(2)
    commands = []
    run_as_module = False
    for opt, optarg in opts:
        if opt in ["-h", "--help"]:
            print(_usage)
            sys.exit()
        elif opt in ["-c", "--command"]:
            commands.append(optarg)
        elif opt in ["-m"]:
            run_as_module = True
    mainpyfile = args[0]
    if not run_as_module and not os.path.exists(mainpyfile):
        print("Error: %s does not exist!" % mainpyfile)
        sys.exit(1)
    sys.argv[:] = args
    if not run_as_module:
        mainpyfile = os.path.realpath(mainpyfile)
        sys.path[0] = os.path.dirname(mainpyfile)
    _pdb_lock.acquire()
    global GLOBAL_PDB
    pdb = Pdb()
    GLOBAL_PDB = pdb
    _pdb_lock.release()
    pdb.rcLines.extend(commands)
    stay_in_pdb = True
    while stay_in_pdb:
        try:
            if run_as_module:
                pdb._runmodule(mainpyfile)
            else:
                pdb._runscript(mainpyfile)
            if pdb._user_requested_quit:
                break
            pdb.print_pdb_continue_line()
            stay_in_pdb = False
        except Restart:
            print("Restarting", mainpyfile, "with arguments:")
            print("\t" + " ".join(sys.argv[1:]))
            stay_in_pdb = True
        except SystemExit:
            print("The program exited via sys.exit(). Exit status:", end=" ")
            print(sys.exc_info()[1])
            stay_in_pdb = False
        except SyntaxError:
            try:
                traceback.print_exc()
            except Exception:
                pass
            sys.exit(1)
            stay_in_pdb = False
        except Exception:
            try:
                traceback.print_exc()
            except Exception:
                pass
            t = sys.exc_info()[2]
            pdb.interaction(None, t)
            pdb.print_pdb_continue_line()
            if pdb.config.post_mortem_restart:
                stay_in_pdb = True
                pdb.config.exception_caught = True
            else:
                stay_in_pdb = False


if __name__ == "__main__":
    # Note! "pdbp.py" will be executed twice if launched as a module
    run_from_main = True
    import pdbp
    pdbp.main()
