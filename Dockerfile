FROM python:3.12-slim
WORKDIR /app
COPY src/odoo_receipt.py src/auth.py src/web_pages.py /app/

# "poll" prints every new confirmed order. Override the command with
# "serve --bind 0.0.0.0" for the web interface and the Odoo button.
ENTRYPOINT ["python", "/app/odoo_receipt.py", "--config", "/config/config.json"]
CMD ["poll", "--interval", "30"]
