import os
os.environ["MSGBOX_SIGNAL_REST_URL"] = "http://127.0.0.1:8080"
os.environ["MSGBOX_SIGNAL_NUMBER"] = "+32456890336"   # your linked number

from messagebox.providers import SignalProvider
p = SignalProvider()
result = p.send_voice("+32456890336", "/Users/jaredwork/Documents/GitHub/button-box/tests/local/test.ogg", lock_wait=None)
print(result)          # SendResult(ok=True, provider_message_id="...", ...)
print(p.receive())     # after the other party replies
