import time
import pyvisa

CONN_LOST = pyvisa.constants.VI_ERROR_CONN_LOST


def call_with_reconnect(obj, fn, max_retries=3, delay_s=1.0):
    """
    Call fn() (a no-arg callable wrapping a pyvisa operation on obj.inst).

    On VI_ERROR_CONN_LOST (e.g. a transient USB-GPIB adapter dropout), reopen
    obj.inst via obj.rm.open_resource(obj.resource) and retry, up to
    max_retries times. Any other VisaIOError is raised immediately.
    """
    for attempt in range(max_retries):
        try:
            return fn()
        except pyvisa.errors.VisaIOError as e:
            if e.error_code == CONN_LOST and attempt < max_retries - 1:
                print(f"{obj.__class__.__name__}: connection lost, "
                      f"reconnecting (attempt {attempt + 1}/{max_retries})")
                time.sleep(delay_s)
                obj.inst = obj.rm.open_resource(obj.resource)
                obj.inst.timeout = obj.timeout
                if getattr(obj, "write_termination", None):
                    obj.inst.write_termination = obj.write_termination
                    obj.inst.read_termination = obj.read_termination
                continue
            raise
