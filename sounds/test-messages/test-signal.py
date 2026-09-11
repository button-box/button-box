import os
os.environ["MSGBOX_SIGNAL_REST_URL"] = "http://127.0.0.1:8080" # or some other address
os.environ["MSGBOX_SIGNAL_NUMBER"] = "<enter your number here>"

from messagebox.providers import SignalProvider
p = SignalProvider()
result = p.send_voice("<enter a recipient number here>", "/path/to/test.ogg", lock_wait=None)
print(result)          # SendResult(ok=True, provider_message_id="...", ...)
print(p.receive())     # after the other party replies
