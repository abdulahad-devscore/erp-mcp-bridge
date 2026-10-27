#!/bin/sh
# Create the Odoo database with the modules we need, once.
set -e
if PGPASSWORD="$PASSWORD" psql -h "$HOST" -U "$USER" -lqt | cut -d '|' -f1 | grep -qw "$ODOO_DB"; then
  echo "Odoo database '$ODOO_DB' exists; skipping init"
  exit 0
fi
echo "Creating Odoo database '$ODOO_DB' (takes a couple of minutes)..."
exec odoo --db_host="$HOST" --db_user="$USER" --db_password="$PASSWORD" -d "$ODOO_DB" \
  -i stock,sale_management,purchase,mrp,account --without-demo=all --stop-after-init
