#include <Python.h>
#include <readline/readline.h>
#include <threads.h>
#include <pthread.h>

#ifdef DEBUG
#define LOG_DEBUG(format, ...) printf("DEBUG: " format "\n", ##__VA_ARGS__)
#else
#define LOG_DEBUG(format, ...)
#endif

#ifdef _READLINE_W_MT
#include <signal.h>
#include <history.h>
#include <locale.h>
#include <_iomodule.h>

#define RESTORE_LOCALE(sl) { setlocale(LC_CTYPE, sl); free(sl); }

static const char* promptstr = "(Pdb+) ";
static const char* stdin_encoding_str = "utf-8";
static const char* stdin_err_str = "surrogateescape";
static const int should_auto_add_history = 1;
static char* completed_input_string;
static volatile sig_atomic_t sigwinch_received;
static thread_local PyThreadState* _py_tstate = NULL;

typedef PyObject* (*encodefunc_t)(PyObject*, PyObject*);
typedef struct textio {
    PyObject_HEAD
    int ok;
    int detached;
    Py_ssize_t chunk_size;
    PyObject* buffer;
    PyObject* encoding;
    PyObject* encoder;
	PyObject* decoder;
    PyObject* readnl;
    PyObject* errors;
    const char* writenl;
    char line_buffering;
    char write_through;
    char readuniversal;
    char readtranslate;
    char writetranslate;
    char seekable;
    char has_read1;
    char telling;
    char finalizing;
    encodefunc_t encodefunc;
    char encoding_start_of_stream;
    PyObject* decoded_chars; 
    Py_ssize_t decoded_chars_used;
    PyObject* pending_bytes;     
    Py_ssize_t pending_bytes_count;
    PyObject* snapshot;
    double b2cratio;
    PyObject* raw;
    PyObject* weakreflist;
    PyObject* dict;
    _PyIO_State* state;
} textio;

static int _get_history_length(void) {
    HISTORY_STATE* hist_st = history_get_history_state();
    int length = hist_st->length;
    free(hist_st);
    return length;
}

static void _rlhandler(char* text) {
    completed_input_string = text;
    rl_callback_handler_remove();
}

static char* _readline_until_enter_or_signal(int* signal) {
    char* not_done_reading = "";
    fd_set selectset;

    *signal = 0;
    rl_catch_signals = 0;

    rl_callback_handler_install(promptstr, _rlhandler);
    FD_ZERO(&selectset);

    completed_input_string = not_done_reading;

    while (completed_input_string == not_done_reading) {
        int has_input = 0, err = 0;

        while (!has_input) {               
			struct timeval timeout = {0, 100000};
            struct timeval *timeoutp = NULL;
            if (sigwinch_received) {
                sigwinch_received = 0;
                rl_resize_terminal();
            }
            FD_SET(fileno(rl_instream), &selectset);
            has_input = select(fileno(rl_instream) + 1, &selectset, NULL, NULL, timeoutp);
            err = errno;
        }

        if (has_input > 0) {
            rl_callback_read_char();
        }
        else if (err == EINTR) {
            int s;
            PyEval_RestoreThread(_py_tstate);
            s = PyErr_CheckSignals();
            if (s < 0) {
                rl_free_line_state();
                rl_callback_sigcleanup();
                rl_cleanup_after_signal();
                rl_callback_handler_remove();
                *signal = 1;
                completed_input_string = NULL;
            }
            PyEval_SaveThread();
        }
    }

    return completed_input_string;
}

static char* _pty_readline_internal(void) {
	size_t n;
	char* p;
	int signal;

    char *saved_locale = strdup(setlocale(LC_CTYPE, NULL));
    if (!saved_locale)
        Py_FatalError("not enough memory to save locale");
    _Py_SetLocaleFromEnv(LC_CTYPE);

    if (f_pty_in != rl_instream || f_pty_out != rl_outstream) {
        rl_instream = f_pty_in;
        rl_outstream = f_pty_out;
        rl_prep_terminal(1);
    }

    p = _readline_until_enter_or_signal(&signal);

    if (signal) {
		RESTORE_LOCALE(saved_locale)
        return NULL;
    }

    if (p == NULL) {
        p = PyMem_RawMalloc(1);
        if (p != NULL)
            *p = '\0';
		RESTORE_LOCALE(saved_locale)
        return p;
    }

    n = strlen(p);
    if (should_auto_add_history && n > 0) {
		LOCK_H;
        const char *line;
        int length = _get_history_length();
        if (length > 0) {
            HIST_ENTRY *hist_ent;
			hist_ent = history_get(length);
            line = hist_ent ? hist_ent->line : "";
        } else
            line = "";
        if (strcmp(p, line))
            add_history(p);
		UNLOCK_H;
    }

    char *q = p;
    p = PyMem_RawMalloc(n+2);
    if (p != NULL) {
        memcpy(p, q, n);
        p[n] = '\n';
        p[n+1] = '\0';
    }
    free(q);
    RESTORE_LOCALE(saved_locale)
    return p;
}

static char* _pty_readline_impl(void) {
	char* rv; 
	char* res;
	size_t len;
    
    PyThreadState *tstate = PyThreadState_GET();
    if (_py_tstate == tstate) {
        PyErr_SetString(PyExc_RuntimeError, "can't re-enter readline");
        return NULL;
    }

	Py_BEGIN_ALLOW_THREADS
    _py_tstate = tstate;

   	rv = _pty_readline_internal(); 

    _py_tstate = NULL;
	Py_END_ALLOW_THREADS

    if (rv == NULL)
        return NULL;

    len = strlen(rv) + 1;
    res = PyMem_Malloc(len);
    if (res != NULL) {
        memcpy(res, rv, len);
    }
    else {
        PyErr_NoMemory();
    }
    PyMem_RawFree(rv);

    return res;
}
	
static PyObject* pty_readline(PyObject* self_m, textio* self, PyObject* const* args, Py_ssize_t nargs) {
	PyObject* result = NULL;
	char* s = NULL;
	size_t len;

    if (!_PyArg_CheckPositional("pty_readline", nargs, 0, 0)) {
        goto exit;
    }

	assert(f_pty_in != NULL && f_pty_out != NULL);
	fflush(f_pty_out);

	s = _pty_readline_impl();
	if (s == NULL) {
		PyErr_CheckSignals();
		if (!PyErr_Occurred())
			PyErr_SetNone(PyExc_KeyboardInterrupt);
		goto exit;
	}

	len = strlen(s);
	if (len == 0) {
		PyErr_SetNone(PyExc_EOFError);
	}
	else {
		if (len > PY_SSIZE_T_MAX) {
			PyErr_SetString(PyExc_OverflowError, "input: input too long");
		}
		else {
			len--;   
			if (len != 0 && s[len-1] == '\r')
				len--;   
			result = PyUnicode_Decode(s, len, stdin_encoding_str, stdin_err_str);
		}
	}
	PyMem_Free(s);

exit:
	return result;
}

#define _MOD_NAME "_rl_patch_mt"
#define _MOD_INIT_FUNC_NAME PyInit__rl_patch_mt
#else
#define _MOD_NAME "_rl_patch"
#define _MOD_INIT_FUNC_NAME PyInit__rl_patch
#endif

static thread_local int f_pty_set = 0;
static thread_local FILE* f_pty_in = NULL; 
static thread_local FILE* f_pty_out = NULL;
static int patched = 0;
static rl_hook_func_t* original_hook = NULL;
static char* (*ori_readline_func)(FILE*, FILE*, const char*) = NULL;
static pthread_mutex_t sethook_mutex = PTHREAD_MUTEX_INITIALIZER;

static int new_hook(void) {
    int rt = 0;	
    if (original_hook != NULL) {
        rt = original_hook();
    }
	rl_tty_set_echoing(1);
	return rt;
}

static char* new_readline_func(FILE* sys_stdin, FILE* sys_stdout, const char* prompt) {
	assert(ori_readline_func != NULL);
	char* rt;
	if ((f_pty_in != NULL) && (f_pty_out != NULL)) {
		rt = ori_readline_func(f_pty_in, f_pty_out, prompt);
	} else {
		rt = ori_readline_func(sys_stdin, sys_stdout, prompt);
	}
	return rt;
}

static PyObject* open_f_pty(PyObject* py_fd) {
	if (!f_pty_set) {
		int fd = (int) PyLong_AsLong(py_fd);
		f_pty_in = fdopen(dup(fd), "r");
		f_pty_out = fdopen(dup(fd), "w");
		f_pty_set = 1;
        LOG_DEBUG("STDIO files set successfully");
    } else {
        LOG_DEBUG("STDIO files already set");
	}
	Py_RETURN_NONE;
}

static PyObject* patch_hook(PyObject* self, PyObject* py_fd) {
    if (!patched) {
		pthread_mutex_lock(&sethook_mutex);
		ori_readline_func = PyOS_ReadlineFunctionPointer;
		PyOS_ReadlineFunctionPointer = new_readline_func;
        original_hook = rl_startup_hook;
        rl_startup_hook = new_hook;
		patched = 1;
		pthread_mutex_unlock(&sethook_mutex);
        LOG_DEBUG("Readline hook patched successfully");
    } else {
        LOG_DEBUG("Readline hook already patched");
	}
	open_f_pty(py_fd);
	Py_RETURN_NONE;
}

static PyObject* close_f_pty(void) {
	if (f_pty_set) {
		fflush(f_pty_out);
		fclose(f_pty_in);
		fclose(f_pty_out);
		f_pty_in = NULL; 
		f_pty_out = NULL;
		f_pty_set = 0;
		LOG_DEBUG("STDIO files unset successfully");
	} else {
		LOG_DEBUG("STDIO files not currently set");
	}
	Py_RETURN_NONE;
}

static PyObject* unpatch_hook(PyObject* self) {
    if (patched) {
		pthread_mutex_lock(&sethook_mutex);
		PyOS_ReadlineFunctionPointer = ori_readline_func;
		rl_startup_hook = original_hook;
        patched = 0;
		pthread_mutex_unlock(&sethook_mutex);
        LOG_DEBUG("Readline hook unpatched successfully");
    } else {
        LOG_DEBUG("Readline hook not currently patched");
    }
	close_f_pty();
	Py_RETURN_NONE;
}

static PyMethodDef module_methods[] = {
    {"patch_hook", (PyCFunction) patch_hook, METH_O, "Patch the readline startup hook"},
    {"unpatch_hook", (PyCFunction) unpatch_hook, METH_NOARGS, "Unpatch the readline startup hook"},
    {"open_f_pty", (PyCFunction) open_f_pty, METH_O, "Open the Pty IO stream"},
    {"close_f_pty", (PyCFunction) close_f_pty, METH_NOARGS, "Close the Pty IO stream"},
#ifdef _READLINE_W_MT 
	{"pty_readline", (PyCFunction) pty_readline, METH_FASTCALL, "Modified multi-thread readline for PTY"},
#endif
	{NULL, NULL, 0, NULL}
};

static struct PyModuleDef rl_patch_module = {
    PyModuleDef_HEAD_INIT,
    _MOD_NAME,
    "Module for patching readline startup",
    -1,
    module_methods
};

PyMODINIT_FUNC _MOD_INIT_FUNC_NAME(void) {
	/* rl_deprep_term_function = NULL; */
    return PyModule_Create(&rl_patch_module);
}
