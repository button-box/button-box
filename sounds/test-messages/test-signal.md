# How to test the signal API client (assuming Docker is installed)

## Docker Command
```bash
  docker run -d --name signal-rest -p 8080:8080 \
    -v "$(pwd)/.signal-rest-data:/home/.local/share/signal-cli" \
    -e MODE=json-rpc \
    bbernhard/signal-cli-rest-api:latest
```

## Signal Linking

Once running, curl the API to retrieve a QR code that you can link to your signal app (generates a photo in wd named `link.png`)
```bash
curl -s "http://127.0.0.1:8080/v1/qrcodelink?device_name=button-box-test" -o link.png
```

To confirm it's linked, run:
```bash
curl http://127.0.0.1:8080/v1/accounts
```

You should see your phone number that you linked to this signal deployment.

## Send a signal message

Update the variables of the python script in `test-signal.py` and run via the python cli in the root directory
