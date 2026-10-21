# Databricks notebook source
# MAGIC %md
# MAGIC # 12 · Grant access to the Genie agent
# MAGIC Gives one or more groups everything they need to use the Genie Cost Estimator Agent:
# MAGIC
# MAGIC | Object | Privilege | Why |
# MAGIC |---|---|---|
# MAGIC | Catalog | `USE CATALOG` | Reach the agent schema |
# MAGIC | Agent schema | `USE SCHEMA`, `SELECT`, `EXECUTE` | Read the agent's tables and run its functions. Schema-level grants are inherited by every table and function, including ones rebuilt later |
# MAGIC | Genie agent | `CAN RUN` | Ask the agent questions (Genie UI and Genie MCP / Copilot) |
# MAGIC | SQL warehouse | `CAN USE` | Optional: the agent already lends its warehouse to users; this also lets them open the agent's SQL |
# MAGIC
# MAGIC Users never need access to `system` tables. The schema holds the negotiated rates the agent quotes, so grant it to
# MAGIC dedicated groups only. Re-running is safe. With `revoke_others = true`, unlisted groups lose their schema grants; unlisted groups on the
# MAGIC agent itself are reported, not removed.

# COMMAND ----------

dbutils.widgets.text("catalog", "", "1. Catalog")
dbutils.widgets.text("schema", "lakemeter", "2. Schema")
dbutils.widgets.text("user_groups", "", "3. Agent user groups (comma-separated)")
dbutils.widgets.text("space_id", "", "4. Genie agent id (blank = find by title)")
dbutils.widgets.text("space_title", "Lakemeter Cost Estimator", "5. Genie agent title")
dbutils.widgets.text("warehouse_id", "", "6. SQL warehouse id (optional: grant CAN USE)")
dbutils.widgets.dropdown("revoke_others", "false", ["false", "true"], "7. Revoke these grants from unlisted groups")

# COMMAND ----------

# MAGIC %md
# MAGIC **First run:** the cell above only creates the parameter widgets at the top of the notebook. Fill in at least
# MAGIC `catalog`, then **Run all** again. In a job, pass the values as notebook parameters instead.

# COMMAND ----------


import json, requests
W = {k: dbutils.widgets.get(k).strip() for k in ("catalog", "schema", "user_groups", "space_id", "space_title", "warehouse_id", "revoke_others")}
if not W["catalog"]:
    msg = "Fill in the catalog widget at the top of the notebook, then run all cells again (or pass it as a job parameter)."
    print(msg); dbutils.notebook.exit(msg)
FQ = f"{W['catalog']}.{W['schema']}"
GROUPS = [g.strip() for g in W["user_groups"].split(",") if g.strip()]
ctx = dbutils.notebook.entry_point.getDbutils().notebook().getContext()
HOST = "https://" + spark.conf.get("spark.databricks.workspaceUrl")
HEADERS = {"Authorization": f"Bearer {ctx.apiToken().get()}"}
if not GROUPS:
    print("user_groups is blank: nothing to grant.")
    dbutils.notebook.exit("no groups")
print(f"schema {FQ} | groups {GROUPS}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Data and functions (Unity Catalog)

# COMMAND ----------

q = lambda name: "`" + name.replace("`", "``") + "`"
for g in GROUPS:
    for stmt in (f"GRANT USE CATALOG ON CATALOG {q(W['catalog'])} TO {q(g)}",
                 f"GRANT USE SCHEMA, SELECT, EXECUTE ON SCHEMA {W['catalog']}.{q(W['schema'])} TO {q(g)}"):
        try:
            spark.sql(stmt); print(stmt)
        except Exception as e:
            if "PRINCIPAL_DOES_NOT_EXIST" in str(e):
                raise ValueError(f"Group {g!r} isn't known to Unity Catalog. Use an account-level group "
                                 "(workspace-local groups such as 'users' can't be granted; use 'account users' instead).") from None
            raise

if W["revoke_others"] == "true":
    keep = {g.lower() for g in GROUPS}
    current = spark.sql(f"SHOW GRANTS ON SCHEMA {FQ}").collect()
    for row in current:
        p, priv = row["Principal"], row["ActionType"]
        if p.lower() not in keep and priv in ("USE SCHEMA", "USE_SCHEMA", "SELECT", "EXECUTE") and row["ObjectType"] == "SCHEMA":
            stmt = f"REVOKE {priv.replace('_', ' ')} ON SCHEMA {FQ} FROM {q(p)}"
            spark.sql(stmt); print(stmt)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. The Genie agent and its warehouse

# COMMAND ----------

space_id = W["space_id"]
if not space_id:
    found, token = [], None
    while True:
        r = requests.get(f"{HOST}/api/2.0/genie/spaces", headers=HEADERS, params={"page_size": 100, **({"page_token": token} if token else {})})
        r.raise_for_status(); page = r.json()
        found += [x["space_id"] for x in page.get("spaces", []) if x.get("title") == W["space_title"]]
        token = page.get("next_page_token")
        if not token: break
    if len(found) != 1:
        raise ValueError(f"{len(found)} Genie agents are titled {W['space_title']!r}: {found}. Set space_id.")
    space_id = found[0]

# Workspace permission lists call the all-users group "users"; Unity Catalog calls it "account users"
WS_GROUPS = ["users" if g.lower() == "account users" else g for g in GROUPS]

def grant(path, level, required=True):
    acl = [{"group_name": g, "permission_level": level} for g in WS_GROUPS]
    r = requests.patch(f"{HOST}/api/2.0/permissions/{path}", headers=HEADERS, json={"access_control_list": acl})
    if not r.ok:
        msg = f"{level} on {path}: {r.status_code} {r.text[:300]}"
        if required: raise RuntimeError(msg)
        print("WARNING (optional grant skipped):", msg); return None
    print(f"{level} on {path} -> {WS_GROUPS}")
    return r.json()

grant(f"genie/{space_id}", "CAN_RUN")
if W["revoke_others"] == "true":
    # Agent permissions are only reported, never removed here: replacing the agent ACL could drop owners or managers
    cur = requests.get(f"{HOST}/api/2.0/permissions/genie/{space_id}", headers=HEADERS).json().get("access_control_list", [])
    extra = [a["group_name"] for a in cur if a.get("group_name") and a["group_name"].lower() not in {g.lower() for g in WS_GROUPS}
             and a["group_name"] != "admins"]
    if extra:
        print("NOTE: these unlisted groups still have access to the agent (remove in the agent's Share dialog if intended):", extra)
if W["warehouse_id"]:
    grant(f"sql/warehouses/{W['warehouse_id']}", "CAN_USE", required=False)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. Effective grants

# COMMAND ----------

display(spark.sql(f"SHOW GRANTS ON SCHEMA {FQ}"))
acl = requests.get(f"{HOST}/api/2.0/permissions/genie/{space_id}", headers=HEADERS).json().get("access_control_list", [])
print("Genie agent", space_id)
for a in acl:
    who = a.get("group_name") or a.get("user_name") or a.get("service_principal_name")
    print(f"  {who:45} {', '.join(sorted({p['permission_level'] for p in a.get('all_permissions', [])}))}")
