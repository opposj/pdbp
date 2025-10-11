#include <Python.h>
#include <readline/readline.h>

#ifdef DEBUG
#define LOG_DEBUG(format, ...) printf("DEBUG: " format "\n", ##__VA_ARGS__)
#else
#define LOG_DEBUG(format, ...)
#endif

static rl_hook_func_t* original_hook = (rl_hook_func_t*) NULL;
static int patched = 0;

static int new_hook(void) {
    int rt = 0;	

    if (original_hook != NULL) {
        rt = original_hook();
    }
	
	rl_tty_set_echoing(1);
    return rt;
}

static PyObject* patch_hook(PyObject* self) {
    if (!patched) {
        original_hook = rl_startup_hook;
        rl_startup_hook = new_hook;
		patched = 1;
        LOG_DEBUG("Input hook patched successfully");
    } else {
        LOG_DEBUG("Input hook already patched");
    }
	Py_RETURN_NONE;
}

static PyObject* unpatch_hook(PyObject* self) {
    if (patched) {
		rl_startup_hook = original_hook;
        patched = 0;
        LOG_DEBUG("Input hook unpatched successfully");
    } else {
        LOG_DEBUG("Input hook not currently patched");
    }
	Py_RETURN_NONE;
}

static PyMethodDef module_methods[] = {
    {"patch_hook", (PyCFunction) patch_hook, METH_NOARGS, "Patch the readline startup hook"},
    {"unpatch_hook", (PyCFunction) unpatch_hook, METH_NOARGS, "Unpatch the readline startup hook"},
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
