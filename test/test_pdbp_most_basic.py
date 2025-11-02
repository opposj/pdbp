import pdbp

def test_ipython(arg_in):
    pdbp.set_trace()
    test_var = "Test Var"
    raise NotImplementedError
    print(arg_in)

test_ipython(114)
print("Done")
