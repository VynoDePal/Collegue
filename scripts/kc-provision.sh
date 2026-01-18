#!/bin/sh
set -eu

log () { echo "[kc-provisioner] $*"; }
log_err () { echo "[kc-provisioner] $*" >&2; }

KC_URL="${KC_URL:-http://keycloak:8080}"
KC_REALM="${KC_REALM:-master}"
KC_ADMIN_USERNAME="${KC_ADMIN_USERNAME:?Missing KC_ADMIN_USERNAME}"
KC_ADMIN_PASSWORD="${KC_ADMIN_PASSWORD:?Missing KC_ADMIN_PASSWORD}"
ADMIN_CLIENT_ID="${ADMIN_CLIENT_ID:-admin-cli}"
PROVISION_CLIENT_ID="${PROVISION_CLIENT_ID:-windsurf}"
REQUIRED_SCOPE="${REQUIRED_SCOPE:-mcp.read}"
WAIT_FOR_CLIENT_SECONDS="${WAIT_FOR_CLIENT_SECONDS:-180}"
SLEEP_BETWEEN_CHECKS="${SLEEP_BETWEEN_CHECKS:-5}"

log "Target realm=$KC_REALM url=$KC_URL clientId=$PROVISION_CLIENT_ID scope=$REQUIRED_SCOPE"

ensure_tools () {
    NEEDS=false
    command -v curl >/dev/null 2>&1 || NEEDS=true
    command -v jq >/dev/null 2>&1 || NEEDS=true
    if [ "$NEEDS" = true ]; then
        log "Installing curl and jq..."
        apk add --no-cache curl jq >/dev/null 2>&1 || {
            log "Failed to install curl/jq"; exit 1;
        }
    fi
}
ensure_tools

until (
    code=$(curl -s -o /dev/null -w "%{http_code}" "$KC_URL/health/ready" || true) && echo "$code" | grep -Eq '^(2|3)[0-9]{2}$'
) || (
    code=$(curl -s -o /dev/null -w "%{http_code}" "$KC_URL" || true) && echo "$code" | grep -Eq '^(2|3)[0-9]{2}$'
); do
    log "Waiting for Keycloak readiness..." 
    sleep 2 
done
log "Keycloak is ready."

ensure_tools

get_admin_token () {
	curl -s -X POST "$KC_URL/realms/master/protocol/openid-connect/token" \
		-d grant_type=password \
		-d client_id="$ADMIN_CLIENT_ID" \
		-d username="$KC_ADMIN_USERNAME" \
		-d password="$KC_ADMIN_PASSWORD" | jq -r '.access_token // empty'
}

refresh_token () {
    NEW_TOKEN="$(get_admin_token)"
    if [ -z "$NEW_TOKEN" ] || [ "$NEW_TOKEN" = "null" ]; then
        log_err "Failed to refresh admin token"
        return 1
    fi
    TOKEN="$NEW_TOKEN"
    AUTH_HEADER="Authorization: Bearer $TOKEN"
}

TOKEN="$(get_admin_token)"
if [ -z "$TOKEN" ] || [ "$TOKEN" = "null" ]; then
	log "Failed to obtain admin token"; exit 1
fi
AUTH_HEADER="Authorization: Bearer $TOKEN"

ensure_client_scope () {
    SCOPE_NAME="$1"
    TMP=$(mktemp)
    CODE=$(curl -s -H "$AUTH_HEADER" -o "$TMP" -w "%{http_code}" "$KC_URL/admin/realms/$KC_REALM/client-scopes?search=$SCOPE_NAME" || true)
    if [ "$CODE" = "401" ]; then
        refresh_token || true
        CODE=$(curl -s -H "$AUTH_HEADER" -o "$TMP" -w "%{http_code}" "$KC_URL/admin/realms/$KC_REALM/client-scopes?search=$SCOPE_NAME" || true)
    fi
    EXIST_ID=$(jq -r --arg n "$SCOPE_NAME" '.[] | select(.name==$n) | .id' < "$TMP" | head -n1)
    rm -f "$TMP"
    if [ -n "$EXIST_ID" ]; then
        log "Client scope '$SCOPE_NAME' already exists"
        return 0
    fi
    BODY=$(printf '{"name":"%s","protocol":"openid-connect"}' "$SCOPE_NAME")
    CREATE_CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST -H "$AUTH_HEADER" -H 'Content-Type: application/json' \
        -d "$BODY" "$KC_URL/admin/realms/$KC_REALM/client-scopes" || true)
    if echo "$CREATE_CODE" | grep -Eq '^(200|201|204)$'; then
        log "Created client scope '$SCOPE_NAME' (HTTP $CREATE_CODE)"
    else
        log_err "Failed to create client scope '$SCOPE_NAME' (HTTP $CREATE_CODE)"
    fi
}

ensure_client_scope "mcp.read"
ensure_client_scope "mcp.write"

ensure_realm_default_scope () {
    SCOPE_NAME="$1"
    SCOPE_ID=$(curl -s -H "$AUTH_HEADER" "$KC_URL/admin/realms/$KC_REALM/client-scopes?search=$SCOPE_NAME" | \
        jq -r --arg n "$SCOPE_NAME" '.[] | select(.name==$n) | .id' | head -n1)
    if [ -z "$SCOPE_ID" ]; then
        log "Scope '$SCOPE_NAME' not found; skipping realm default attachment"
        return 0
    fi
    TMP=$(mktemp)
    CODE=$(curl -s -H "$AUTH_HEADER" -o "$TMP" -w "%{http_code}" "$KC_URL/admin/realms/$KC_REALM/default-default-client-scopes" || true)
    if [ "$CODE" = "401" ]; then
        refresh_token || true
        CODE=$(curl -s -H "$AUTH_HEADER" -o "$TMP" -w "%{http_code}" "$KC_URL/admin/realms/$KC_REALM/default-default-client-scopes" || true)
    fi
    PRESENT=$(jq -r --arg id "$SCOPE_ID" '.[] | select(.id==$id) | .id' < "$TMP" | head -n1)
    rm -f "$TMP"
    if [ -n "$PRESENT" ]; then
        log "Client scope '$SCOPE_NAME' is already set as realm Default"
        return 0
    fi
    ADD_CODE=$(curl -s -o /dev/null -w "%{http_code}" -X PUT -H "$AUTH_HEADER" \
        "$KC_URL/admin/realms/$KC_REALM/default-default-client-scopes/$SCOPE_ID" || true)
    if echo "$ADD_CODE" | grep -Eq '^(200|201|204)$'; then
        log "Marked client scope '$SCOPE_NAME' as realm Default (HTTP $ADD_CODE)"
    else
        ADD_CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST -H "$AUTH_HEADER" \
            "$KC_URL/admin/realms/$KC_REALM/default-default-client-scopes/$SCOPE_ID" || true)
        if echo "$ADD_CODE" | grep -Eq '^(200|201|204)$'; then
            log "Marked client scope '$SCOPE_NAME' as realm Default via POST (HTTP $ADD_CODE)"
        else
            log_err "Failed to mark '$SCOPE_NAME' as realm Default (HTTP $ADD_CODE)"
        fi
    fi
}

ensure_realm_default_scope "mcp.read"
ensure_realm_default_scope "mcp.write"

find_client_id () {
    # 1) Try exact clientId
    TMP=$(mktemp)
    CODE=$(curl -s -H "$AUTH_HEADER" -o "$TMP" -w "%{http_code}" "$KC_URL/admin/realms/$KC_REALM/clients?clientId=$PROVISION_CLIENT_ID" || true)
    if [ "$CODE" = "401" ]; then
        refresh_token || true
        CODE=$(curl -s -H "$AUTH_HEADER" -o "$TMP" -w "%{http_code}" "$KC_URL/admin/realms/$KC_REALM/clients?clientId=$PROVISION_CLIENT_ID" || true)
    fi
    if echo "$CODE" | grep -Eq '^(2|3)[0-9]{2}$'; then
        CID=$(jq -r 'if (type=="array") and (length>0) then .[0].id else "" end' < "$TMP")
    else
        CID=""
    fi
    rm -f "$TMP"
    if [ -n "$CID" ]; then
        echo "$CID"; return 0
    fi

    # 2) Fallback: search by name/clientId (DCR may set a random clientId, keeping the name)
    TMP=$(mktemp)
    CODE=$(curl -s -H "$AUTH_HEADER" -o "$TMP" -w "%{http_code}" "$KC_URL/admin/realms/$KC_REALM/clients?search=$PROVISION_CLIENT_ID" || true)
    if [ "$CODE" = "401" ]; then
        refresh_token || true
        CODE=$(curl -s -H "$AUTH_HEADER" -o "$TMP" -w "%{http_code}" "$KC_URL/admin/realms/$KC_REALM/clients?search=$PROVISION_CLIENT_ID" || true)
    fi
    if echo "$CODE" | grep -Eq '^(2|3)[0-9]{2}$'; then
        CID=$(jq -r --arg q "$PROVISION_CLIENT_ID" '
            map(select((.clientId==$q) or ((.name//"")==$q) or 
                       ((.name//"")|contains($q)) or ((.clientId//"")|contains($q))))
            | if (length>0) then .[0].id else "" end' < "$TMP")
    else
        CID=""
    fi
    rm -f "$TMP"
    if [ -z "$CID" ]; then
        if [ $((ELAPSED % 30)) -eq 0 ]; then
            log_err "clients query failed or not found (HTTP $CODE). Showing top matches:"
            TMP=$(mktemp)
            curl -s -H "$AUTH_HEADER" "$KC_URL/admin/realms/$KC_REALM/clients?search=$PROVISION_CLIENT_ID" > "$TMP" || true
            jq -r --arg q "$PROVISION_CLIENT_ID" 'map({name: (.name//""), clientId, id}) | .[0:5][] | "- name=\(.name) clientId=\(.clientId) id=\(.id)"' < "$TMP" | sed 's/^/[kc-provisioner] /' 1>&2 || true
            rm -f "$TMP"
        fi
    fi
    echo "$CID"
}

CID=""
ELAPSED=0
while [ $ELAPSED -lt $WAIT_FOR_CLIENT_SECONDS ]; do
    CID="$(find_client_id)"
    if [ -n "$CID" ]; then
        break
    fi
    log "Client '$PROVISION_CLIENT_ID' not found yet. Waiting... ($ELAPSED s)"
    sleep "$SLEEP_BETWEEN_CHECKS"
    ELAPSED=$((ELAPSED + SLEEP_BETWEEN_CHECKS))
done

if [ -z "$CID" ]; then
    log "Client '$PROVISION_CLIENT_ID' not found after $WAIT_FOR_CLIENT_SECONDS seconds. Exiting."
    exit 0
fi
log "Found clientId='$PROVISION_CLIENT_ID' id='$CID'"

CLIENT_REP="$(curl -s -H "$AUTH_HEADER" "$KC_URL/admin/realms/$KC_REALM/clients/$CID")"
NEW_REP="$(echo "$CLIENT_REP" | jq '.serviceAccountsEnabled = true | .publicClient = false | .fullScopeAllowed = false | .clientAuthenticatorType = "client-secret"')"

if [ "$(echo "$CLIENT_REP" | jq -r '.serviceAccountsEnabled')" != "true" ] || \
   [ "$(echo "$CLIENT_REP" | jq -r '.publicClient')" = "true" ] || \
   [ "$(echo "$CLIENT_REP" | jq -r '.fullScopeAllowed')" = "true" ] || \
   [ "$(echo "$CLIENT_REP" | jq -r '.clientAuthenticatorType')" != "client-secret" ]; then
    log "Updating client to enable service accounts, confidential type and disable full scope..."
    curl -s -X PUT -H "$AUTH_HEADER" -H 'Content-Type: application/json' \
        -d "$NEW_REP" "$KC_URL/admin/realms/$KC_REALM/clients/$CID" >/dev/null
else
    log "Client already has desired base settings"
fi

TMP=$(mktemp)
HTTP_CODE=$(curl -s -H "$AUTH_HEADER" -o "$TMP" -w "%{http_code}" "$KC_URL/admin/realms/$KC_REALM/clients/$CID/client-secret" || true)
if [ "$HTTP_CODE" = "401" ]; then
    refresh_token || true
    HTTP_CODE=$(curl -s -H "$AUTH_HEADER" -o "$TMP" -w "%{http_code}" "$KC_URL/admin/realms/$KC_REALM/clients/$CID/client-secret" || true)
fi
if echo "$HTTP_CODE" | grep -Eq '^(2|3)[0-9]{2}$'; then
    SECRET_VALUE="$(jq -r '.value // empty' < "$TMP")"
else
    SECRET_VALUE=""
fi
rm -f "$TMP"
if [ -z "$SECRET_VALUE" ]; then
    log "Generating client secret..."
    TMP=$(mktemp)
    HTTP_CODE=$(curl -s -X POST -H "$AUTH_HEADER" -o "$TMP" -w "%{http_code}" "$KC_URL/admin/realms/$KC_REALM/clients/$CID/client-secret" || true)
    if [ "$HTTP_CODE" = "401" ]; then
        refresh_token || true
        HTTP_CODE=$(curl -s -X POST -H "$AUTH_HEADER" -o "$TMP" -w "%{http_code}" "$KC_URL/admin/realms/$KC_REALM/clients/$CID/client-secret" || true)
    fi
    if echo "$HTTP_CODE" | grep -Eq '^(2|3)[0-9]{2}$'; then
        SECRET_VALUE="$(jq -r '.value // empty' < "$TMP")"
        if [ -z "$SECRET_VALUE" ]; then
            rm -f "$TMP"
            TMP=$(mktemp)
            READ_CODE=$(curl -s -H "$AUTH_HEADER" -o "$TMP" -w "%{http_code}" "$KC_URL/admin/realms/$KC_REALM/clients/$CID/client-secret" || true)
            if [ "$READ_CODE" = "401" ]; then
                refresh_token || true
                READ_CODE=$(curl -s -H "$AUTH_HEADER" -o "$TMP" -w "%{http_code}" "$KC_URL/admin/realms/$KC_REALM/clients/$CID/client-secret" || true)
            fi
            if echo "$READ_CODE" | grep -Eq '^(2|3)[0-9]{2}$'; then
                SECRET_VALUE="$(jq -r '.value // empty' < "$TMP")"
            fi
        fi
    else
        SECRET_VALUE=""
    fi
    rm -f "$TMP"
    if [ -n "$SECRET_VALUE" ]; then
        log "Client secret generated (masked): ******${SECRET_VALUE#${SECRET_VALUE%????}}"
    else
        log_err "Failed to generate client secret (HTTP $HTTP_CODE)"
    fi
else
    log "Client already has a secret"
fi

attach_client_default_scope () {
    SCOPE_NAME="$1"
    TMP=$(mktemp)
    HTTP_CODE=$(curl -s -H "$AUTH_HEADER" -o "$TMP" -w "%{http_code}" "$KC_URL/admin/realms/$KC_REALM/client-scopes?search=$SCOPE_NAME" || true)
    if [ "$HTTP_CODE" = "401" ]; then
        refresh_token || true
        HTTP_CODE=$(curl -s -H "$AUTH_HEADER" -o "$TMP" -w "%{http_code}" "$KC_URL/admin/realms/$KC_REALM/client-scopes?search=$SCOPE_NAME" || true)
    fi
    if echo "$HTTP_CODE" | grep -Eq '^(2|3)[0-9]{2}$'; then
        SCOPE_ID="$(jq -r --arg n "$SCOPE_NAME" '.[] | select(.name==$n) | .id' < "$TMP" | head -n1)"
    else
        SCOPE_ID=""
    fi
    rm -f "$TMP"
    if [ -n "$SCOPE_ID" ]; then
        TMP=$(mktemp)
        HTTP_CODE=$(curl -s -H "$AUTH_HEADER" -o "$TMP" -w "%{http_code}" "$KC_URL/admin/realms/$KC_REALM/clients/$CID/default-client-scopes" || true)
        if [ "$HTTP_CODE" = "401" ]; then
            refresh_token || true
            HTTP_CODE=$(curl -s -H "$AUTH_HEADER" -o "$TMP" -w "%{http_code}" "$KC_URL/admin/realms/$KC_REALM/clients/$CID/default-client-scopes" || true)
        fi
        if echo "$HTTP_CODE" | grep -Eq '^(2|3)[0-9]{2}$'; then
            ATTACHED="$(jq -r '.[].name' < "$TMP" | grep -x "$SCOPE_NAME" || true)"
        else
            ATTACHED=""
        fi
        rm -f "$TMP"
        if [ -z "$ATTACHED" ]; then
            log "Attaching default client scope '$SCOPE_NAME'..."
            HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" -X PUT -H "$AUTH_HEADER" "$KC_URL/admin/realms/$KC_REALM/clients/$CID/default-client-scopes/$SCOPE_ID")
            if [ "$HTTP_CODE" = "401" ]; then
                refresh_token || true
                HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" -X PUT -H "$AUTH_HEADER" "$KC_URL/admin/realms/$KC_REALM/clients/$CID/default-client-scopes/$SCOPE_ID")
            fi
            log "Attach scope '$SCOPE_NAME' response: $HTTP_CODE"
        else
            log "Scope '$SCOPE_NAME' already attached"
        fi
    else
        log "Scope '$SCOPE_NAME' not found in realm; skipping attachment"
    fi
}

attach_client_default_scope "mcp.read"
attach_client_default_scope "mcp.write"

log "Provisioning completed successfully."
