FROM python:3.12-slim

WORKDIR /app

COPY src/odoo_receipt.py /app/odoo_receipt.py

# "poll" prints every new confirmed order. Override the command with
# "serve --bind 0.0.0.0" for the button in the Odoo backend.
ENTRYPOINT ["python", "/app/odoo_receipt.py", "--config", "/config/config.json"]
CMD ["poll", "--interval", "30"]
