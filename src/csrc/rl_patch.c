#include <Python.h>
#include <readline/readline.h>
#include <threads.h>
#include <pthread.h>

#ifdef DEBUG
#define LOG_DEBUG(format, ...) printf("DEBUG: " format "\n", ##__VA_ARGS__)
#else
#define LOG_DEBUG(format, ...)
#endif

static thread_local int f_pty_set = 0;
static thread_local FILE* f_pty_in = NULL; 
static thread_local FILE* f_pty_out = NULL;
static int patched = 0;
static rl_hook_func_t* original_hook = NULL;
static char* (*ori_readline_func)(FILE*, FILE*, const char*) = NULL;
static pthread_mutex_t sethook_mutex = PTHREAD_MUTEX_INITIALIZER;
static pthread_mutex_t readline_mutex = PTHREAD_MUTEX_INITIALIZER;

static int new_hook(void) {
    int rt = 0;	
    if (original_hook != NULL) {
        rt = original_hook();
    }
	rl_tty_set_echoing(1);
	return rt;
}

static char* new_readline_func(FILE* sys_stdin, FILE* sys_stdout, const char* prompt) {
	pthread_mutex_lock(&readline_mutex);
	assert(ori_readline_func != NULL);
	char* rt;
	if ((f_pty_in != NULL) && (f_pty_out != NULL)) {
		rt = ori_readline_func(f_pty_in, f_pty_out, prompt);
	} else {
		rt = ori_readline_func(sys_stdin, sys_stdout, prompt);
	}
	pthread_mutex_unlock(&readline_mutex);
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
    {NULL, NULL, 0, NULL}
};

static struct PyModuleDef rl_patch_module = {
    PyModuleDef_HEAD_INIT,
    "_rl_patch",
    "Module for patching readline startup",
    -1,
    module_methods
};

PyMODINIT_FUNC PyInit__rl_patch(void) {
    return PyModule_Create(&rl_patch_module);
}
