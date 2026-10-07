%md
# DCP Smart Estimation Calculator v3.8 | Technical Preview
One consolidated notebook: prefilled SEND intake, multi-component interface, compute selection, live rate discovery, read-only historical calibration, concurrency-aware SQL sizing, S3 sensitivity, monthly component roll-up, publication output, and learning output.

Guardrails: no cache/persist, no system-table writes, no automatic sandbox refresh, Non-Production primary, Work/Genie/KNN independent challengers.


---cell 1 --
from dataclasses import dataclass
from typing import List, Optional
from statistics import median
from pyspark.sql import functions as F, types as T
from pyspark.sql.window import Window
import math

VERSION="3.8"; PRIMARY_WORKSPACE_ID="4357490608420283"; WORK_WORKSPACE_ID="2042810613425161"; LOOKBACK_DAYS=90
SCENARIOS=["LOW","EXPECTED","HIGH","STRESS"]
GOVERNED_RATES={"JOBS_SERVERLESS":0.3465,"SQL_PRO":0.4235}
AWS_RATES={"DATASYNC_GB":0.0125,"S3_GB_MONTH":0.023,"S3_PUT_LIST_1000":0.005,"S3_GET_1000":0.0004}
CONTINUOUS_HOURS_MONTH=730.0
CONTINUOUS_DBU_H={"LOW":0.5,"EXPECTED":1.0,"HIGH":1.5,"STRESS":2.0}
BATCH_DBU_H=1.0; SQL_BASE_DBU_H=4.0
SQL_ACTIVE_HOURS_DAY={"LOW":0.5,"EXPECTED":1.0,"HIGH":2.0,"STRESS":4.0}
SQL_CONCURRENCY_FACTOR={"LOW":0.5,"EXPECTED":1.0,"HIGH":1.5,"STRESS":2.0}
SQL_USERS_PER_CAPACITY_BAND=10
EVENTS={"LOW":0,"EXPECTED":1,"HIGH":1,"STRESS":4}

@dataclass
class Component:
    component_id:str; name:str; workload_type:str; quantity:int=1; pattern:str="TBD"
    runtime_low:Optional[float]=None; runtime_expected:Optional[float]=None; runtime_high:Optional[float]=None
    confirmed_events_month:Optional[float]=None; evidence:str=""
    requires_gpu:bool=False; requires_custom_runtime:bool=False; requires_custom_networking:bool=False; declarative_pipeline_preferred:bool=False
    streaming_mode:str="UNCONFIRMED"; latency_requirement:str="UNCONFIRMED"

@dataclass
class Intake:
    use_case_id:str; use_case_name:str; source:str; destination:str; current_gb:float; datasets:int; tables:int
    daily_low:float; daily_expected:float; daily_high:float; planning_days:int; dashboard_availability_hours:float
    total_users:int; concurrent_users:int; reporting_pattern:str; components:List[Component]
    include_initial_transfer:bool=False; retention_days:Optional[float]=None; average_object_mb:Optional[float]=None

SEND_US01=Intake("SEND_US01","Reporting and Reconciliation","OZONE","AWS_S3_FOR_DATABRICKS",5000,5,100,6,8,10,22,8,50,10,"ONCE_DAILY_PLUS_ON_DEMAND",[
 Component("SEND_US01-C01","Continuous streaming data processing","CONTINUOUS_STREAMING",1,"CONTINUOUS",evidence="One continuously running workload supporting Bronze, Silver, and Gold processing."),
 Component("SEND_US01-C02","Event-driven batch resiliency and backfill","EVENT_DRIVEN_BATCH",1,"AS_NEEDED",1,3,5,None,"One batch workload runs 1 to 5 hours when backfill is needed; frequency unconfirmed.")])
FORM_STATE={"intake":SEND_US01}


---
%md
## 1. Live rate registry
Effective Databricks list rates are discovered read-only. Confirmed account rates remain governed overrides. DataSync and S3 stay in a separate AWS registry.

--cell 2 --
def resolve_rates():
    base=spark.createDataFrame([
      ("JOBS_SERVERLESS",None,"DBU",0.3465,"GOVERNED_ACCOUNT_OVERRIDE","USD"),
      ("SQL_PRO",None,"DBU",0.4235,"GOVERNED_ACCOUNT_OVERRIDE","USD"),
      ("AWS_DATASYNC",None,"GB",0.0125,"EXTERNAL_AWS_REFERENCE","USD")],
      "service_key string, sku_name string, usage_unit string, selected_rate double, rate_source string, currency string")
    try:
      lp=spark.table("system.billing.list_prices")
      pricing=F.coalesce(F.col("pricing.effective_list.default"),F.col("pricing.default")).cast("double")
      live=(lp.where(F.upper("cloud")=="AWS").where(F.col("price_start_time")<=F.current_timestamp())
        .where(F.col("price_end_time").isNull()|(F.col("price_end_time")>F.current_timestamp()))
        .select(F.upper("sku_name").alias("sku_name"),"usage_unit",F.col("currency_code").alias("currency"),pricing.alias("selected_rate"),"price_start_time")
        .where(F.col("selected_rate").isNotNull()).withColumn("rn",F.row_number().over(Window.partitionBy("sku_name","usage_unit").orderBy(F.desc("price_start_time")))).where("rn=1")
        .select(F.lit("DISCOVERED_DATABRICKS_SKU").alias("service_key"),"sku_name","usage_unit","selected_rate",F.lit("SYSTEM_EFFECTIVE_LIST").alias("rate_source"),"currency"))
      return base.unionByName(live)
    except Exception as e:
      print("Live list-price discovery unavailable; governed rates remain active:",str(e)[:240]); return base
resolved_rate_registry_df=resolve_rates(); display(resolved_rate_registry_df.orderBy("service_key","sku_name"))

---
%md
## 2. Governed compute-selection engine
        
--- cell 3 --

REGISTRY={
 "JOBS_SERVERLESS":dict(types=["CONTINUOUS_STREAMING","SCHEDULED_BATCH","EVENT_DRIVEN_BATCH","ML_TRAINING","BATCH_SCORING"],score=70,rate=.3465,model="DBU",verified=True),
 "CLASSIC_JOBS_COMPUTE":dict(types=["CONTINUOUS_STREAMING","SCHEDULED_BATCH","EVENT_DRIVEN_BATCH","ML_TRAINING","BATCH_SCORING"],score=45,rate=None,model="DBU_PLUS_AWS_REQUIRED",verified=True),
 "SERVERLESS_PIPELINE":dict(types=["CONTINUOUS_STREAMING","SCHEDULED_BATCH"],score=55,rate=None,model="RATE_REQUIRED",verified=False),
 "SQL_PRO":dict(types=["SQL_REPORTING","INTERACTIVE_ANALYTICS"],score=70,rate=.4235,model="DBU",verified=True),
 "SERVERLESS_SQL":dict(types=["SQL_REPORTING","INTERACTIVE_ANALYTICS"],score=65,rate=None,model="RATE_REQUIRED",verified=False),
 "GPU_JOBS_COMPUTE":dict(types=["ML_TRAINING","BATCH_SCORING"],score=35,rate=None,model="GPU_RATE_REQUIRED",verified=False),
 "MODEL_SERVING":dict(types=["MODEL_SERVING"],score=80,rate=None,model="SERVING_RATE_REQUIRED",verified=False),
 "VECTOR_SEARCH":dict(types=["VECTOR_SEARCH"],score=80,rate=None,model="VECTOR_RATE_REQUIRED",verified=False),
 "FOUNDATION_MODEL_API":dict(types=["FOUNDATION_MODEL_API"],score=80,rate=None,model="TOKEN_RATE_REQUIRED",verified=False)}

def recommend(c):
    if c.workload_type=="CONTINUOUS_STREAMING":
      if c.requires_custom_runtime or c.requires_custom_networking:
        return dict(selected="CLASSIC_JOBS_COMPUTE",alternative="SERVERLESS_PIPELINE",score=90,confidence="MEDIUM",status="CUSTOM_REQUIREMENT_CONFIRMED_COST_INPUTS_REQUIRED",rate=None,model="DBU_PLUS_AWS_REQUIRED",eligible=["CLASSIC_JOBS_COMPUTE","SERVERLESS_PIPELINE","JOBS_SERVERLESS"],streaming_decision="CUSTOM_REQUIREMENTS")
      if c.streaming_mode=="TRUE_CONTINUOUS_LOW_LATENCY" or c.latency_requirement=="SECONDS" or c.declarative_pipeline_preferred:
        return dict(selected="SERVERLESS_PIPELINE",alternative="JOBS_SERVERLESS",score=90,confidence="HIGH",status="PIPELINE_RATE_REQUIRED",rate=None,model="RATE_REQUIRED",eligible=["SERVERLESS_PIPELINE","JOBS_SERVERLESS","CLASSIC_JOBS_COMPUTE"],streaming_decision="TRUE_CONTINUOUS_LOW_LATENCY")
      if c.streaming_mode=="INCREMENTAL_AVAILABLE_NOW" or c.latency_requirement=="MINUTES_OR_MORE":
        return dict(selected="JOBS_SERVERLESS",alternative="SERVERLESS_PIPELINE",score=90,confidence="HIGH",status="COST_MODEL_READY",rate=GOVERNED_RATES["JOBS_SERVERLESS"],model="DBU",eligible=["JOBS_SERVERLESS","SERVERLESS_PIPELINE","CLASSIC_JOBS_COMPUTE"],streaming_decision="INCREMENTAL_AVAILABLE_NOW")
      return dict(selected="JOBS_SERVERLESS",alternative="SERVERLESS_PIPELINE",score=70,confidence="MEDIUM",status="CONDITIONAL_JOBS_SERVERLESS_PRICING_PROXY_PENDING_STREAMING_CLASSIFICATION",rate=GOVERNED_RATES["JOBS_SERVERLESS"],model="DBU_PLANNING_PROXY",eligible=["SERVERLESS_PIPELINE","JOBS_SERVERLESS","CLASSIC_JOBS_COMPUTE"],streaming_decision="UNCONFIRMED")
    a=[]
    for n,o in REGISTRY.items():
      if c.workload_type not in o["types"]: continue
      score=o["score"]; reasons=[]
      if n=="JOBS_SERVERLESS":
        if c.pattern in ["CONTINUOUS","SCHEDULED","AS_NEEDED"]: score+=15; reasons.append("Fits automated execution")
        if c.requires_custom_runtime or c.requires_custom_networking: score-=45; reasons.append("Custom configuration reduces serverless fit")
        if c.requires_gpu: score-=40
      if n=="CLASSIC_JOBS_COMPUTE": score+=45 if (c.requires_custom_runtime or c.requires_custom_networking) else -15
      if n=="SERVERLESS_PIPELINE": score+=35 if c.declarative_pipeline_preferred else -20
      if n=="GPU_JOBS_COMPUTE": score+=50 if c.requires_gpu else -50
      if o["rate"] is None: reasons.append("Final cost inputs required")
      a.append((score,n,o,reasons))
    a.sort(key=lambda x:(-x[0],x[1]))
    if not a:return dict(selected="ARCHITECTURE_REVIEW_REQUIRED",alternative=None,score=None,confidence="LOW",status="NO_ELIGIBLE_OPTION",rate=None,model=None,eligible=[])
    b=a[0]; alt=a[1] if len(a)>1 else None
    confidence="HIGH" if b[0]>=85 and b[2]["verified"] else ("MEDIUM" if b[0]>=60 else "LOW")
    status="COST_MODEL_READY" if b[2]["rate"] is not None and "REQUIRED" not in b[2]["model"] else "RECOMMENDATION_READY_COST_INPUTS_REQUIRED"
    return dict(selected=b[1],alternative=None if alt is None else alt[1],score=b[0],confidence=confidence,status=status,rate=b[2]["rate"],model=b[2]["model"],eligible=[x[1] for x in a])

def backfill_confidence(basis, peers, frequency_confirmed):
    rate_confidence = "HIGH" if basis.startswith("HISTORICALLY") and peers >= 5 else ("MEDIUM" if basis.startswith("HISTORICALLY") and peers >= 3 else "LOW")
    frequency_confidence = "HIGH" if frequency_confirmed else "LOW"
    overall = "MEDIUM" if rate_confidence in ["HIGH", "MEDIUM"] and frequency_confirmed else "LOW"
    return rate_confidence, frequency_confidence, overall

---
%md
## 3. Prefilled multi-component interface

---- cell 4 ---

try:
 import ipywidgets as w
 from IPython.display import display as show, clear_output
 rows=[]; box=w.VBox(); msg=w.Output()
 f={"id":w.Text(value="SEND_US01",description="Use case"),"name":w.Text(value="Reporting and Reconciliation",description="Name"),"gb":w.FloatText(value=5000,description="Current GB"),"sets":w.IntText(value=5,description="Datasets"),"tables":w.IntText(value=100,description="Tables"),"lo":w.FloatText(value=6,description="GB/day low"),"ex":w.FloatText(value=8,description="Expected"),"hi":w.FloatText(value=10,description="High"),"days":w.IntText(value=22,description="Work days"),"avail":w.FloatText(value=8,description="Avail hrs"),"users":w.IntText(value=50,description="Users"),"conc":w.IntText(value=10,description="Concurrent"),"initial":w.Checkbox(value=False,description="Include initial transfer"),"ret":w.Text(value="",description="Retention days"),"obj":w.Text(value="",description="Avg object MB")}
 def refresh():box.children=tuple(r["panel"] for r in rows)
 def add(_,c=None):
  r={};r["id"]=w.Text(value=c.component_id if c else f"{f['id'].value}-C{len(rows)+1:02d}",description="ID");r["name"]=w.Text(value=c.name if c else "New component",description="Component")
  opts=sorted({t for o in REGISTRY.values() for t in o["types"]});r["type"]=w.Dropdown(options=opts,value=c.workload_type if c else "SCHEDULED_BATCH",description="Workload");r["qty"]=w.IntText(value=c.quantity if c else 1,description="Qty");r["pattern"]=w.Text(value=c.pattern if c else "TBD",description="Pattern")
  r["rlo"]=w.FloatText(value=c.runtime_low or 0 if c else 0,description="Run low");r["rex"]=w.FloatText(value=c.runtime_expected or 0 if c else 0,description="Run exp");r["rhi"]=w.FloatText(value=c.runtime_high or 0 if c else 0,description="Run high");r["events"]=w.Text(value="" if c is None or c.confirmed_events_month is None else str(c.confirmed_events_month),description="Events/mo")
  r["gpu"]=w.Checkbox(value=c.requires_gpu if c else False,description="GPU");r["custom"]=w.Checkbox(value=c.requires_custom_runtime if c else False,description="Custom runtime");r["net"]=w.Checkbox(value=c.requires_custom_networking if c else False,description="Custom network");r["pipe"]=w.Checkbox(value=c.declarative_pipeline_preferred if c else False,description="Declarative pipeline");r["stream_mode"]=w.Dropdown(options=["UNCONFIRMED","TRUE_CONTINUOUS_LOW_LATENCY","INCREMENTAL_AVAILABLE_NOW"],value=c.streaming_mode if c else "UNCONFIRMED",description="Stream mode");r["latency"]=w.Dropdown(options=["UNCONFIRMED","SECONDS","MINUTES_OR_MORE"],value=c.latency_requirement if c else "UNCONFIRMED",description="Latency");r["ev"]=w.Text(value=c.evidence if c else "",description="Evidence");remove=w.Button(description="Remove",button_style="danger")
  r["panel"]=w.VBox([w.HBox([r["id"],r["name"],r["type"],r["qty"]]),w.HBox([r["pattern"],r["rlo"],r["rex"],r["rhi"],r["events"]]),w.HBox([r["gpu"],r["custom"],r["net"],r["pipe"]]),w.HBox([r["stream_mode"],r["latency"]]),w.HBox([r["ev"],remove])],layout=w.Layout(border="1px solid #bbb",padding="6px"));remove.on_click(lambda _:(rows.remove(r),refresh()));rows.append(r);refresh()
 for c in SEND_US01.components:add(None,c)
 def opt(x):return None if not x.value.strip() else float(x.value)
 def apply(_):
  comps=[]
  for r in rows:
   ev=None if not r["events"].value.strip() else float(r["events"].value)
   comps.append(Component(r["id"].value,r["name"].value,r["type"].value,r["qty"].value,r["pattern"].value,r["rlo"].value or None,r["rex"].value or None,r["rhi"].value or None,ev,r["ev"].value,r["gpu"].value,r["custom"].value,r["net"].value,r["pipe"].value,r["stream_mode"].value,r["latency"].value))
  errors=[]
  if not comps:errors.append("At least one component is required")
  if len({c.component_id for c in comps})!=len(comps):errors.append("Component IDs must be unique")
  for c in comps:
   if c.quantity<1:errors.append(f"{c.component_id}: quantity must be positive")
   if c.workload_type=="EVENT_DRIVEN_BATCH" and (c.runtime_low is None or c.runtime_high is None):errors.append(f"{c.component_id}: runtime range required")
  with msg:
   clear_output()
   if errors:[print("-",x) for x in errors]
   else:FORM_STATE["intake"]=Intake(f["id"].value,f["name"].value,"OZONE","AWS_S3_FOR_DATABRICKS",f["gb"].value,f["sets"].value,f["tables"].value,f["lo"].value,f["ex"].value,f["hi"].value,f["days"].value,f["avail"].value,f["users"].value,f["conc"].value,"ONCE_DAILY_PLUS_ON_DEMAND",comps,f["initial"].value,opt(f["ret"]),opt(f["obj"]));print(f"Applied {len(comps)} components")
 addb=w.Button(description="+ Add workload component",button_style="info");addb.on_click(add);applyb=w.Button(description="Apply inputs",button_style="success");applyb.on_click(apply)
 show(w.VBox([w.HTML("<h3>DCP Smart Estimation Calculator v3.8</h3>"),w.HBox([f["id"],f["name"]]),w.HBox([f["gb"],f["sets"],f["tables"]]),w.HBox([f["lo"],f["ex"],f["hi"],f["days"]]),w.HBox([f["avail"],f["users"],f["conc"]]),w.HBox([f["initial"],f["ret"],f["obj"]]),w.HTML("<h4>Workload components</h4>"),box,w.HBox([addb,applyb]),msg]))
except Exception as e:print("Interactive interface unavailable; SEND preset active:",str(e)[:200])

-----
%md
## 4. Read-only historical calibration

----- cell 5 ---

def quantiles(v):
 a=sorted(float(x) for x in v if x is not None and math.isfinite(float(x)))
 if not a:return (None,None,None)
 def q(p):z=(len(a)-1)*p;i=math.floor(z);j=math.ceil(z);return a[i] if i==j else a[i]+(a[j]-a[i])*(z-i)
 return q(.25),q(.5),q(.75)
def calibrate(ws):
 out=dict(status="FAILED",continuous_peers=0,continuous=(None,None,None),batch_peers=0,batch=(None,None,None),sql_peers=0,sql=(None,None,None),peak_statements=None,ten_users_validated=None)
 try:
  u=(spark.table("system.billing.usage").where(F.col("workspace_id").cast("string")==ws).where(F.col("usage_start_time")>=F.date_sub(F.current_timestamp(),LOOKBACK_DAYS)).select("usage_start_time","usage_end_time",F.col("usage_quantity").cast("double").alias("dbu"),F.upper("sku_name").alias("sku"),F.coalesce(F.col("usage_metadata.job_id").cast("string"),F.col("usage_metadata.cluster_id").cast("string")).alias("resource"),F.col("usage_metadata.warehouse_id").cast("string").alias("warehouse"),F.col("billing_origin_product").cast("string").alias("origin")))
  rr=(u.where(F.col("sku").contains("JOBS")&F.col("resource").isNotNull()).groupBy("resource").agg(F.sum("dbu").alias("dbu"),F.countDistinct(F.to_date("usage_start_time")).alias("days"),F.sum((F.unix_timestamp("usage_end_time")-F.unix_timestamp("usage_start_time"))/3600).alias("hours")).withColumn("hpd",F.col("hours")/F.greatest("days",F.lit(1))))
  cont=[x[0] for x in rr.where((F.col("days")>=60)&(F.col("hpd")>=18)).select(F.col("dbu")*30/LOOKBACK_DAYS).collect()];batch=[x[0] for x in rr.where((F.col("days")<=10)&(F.col("hpd")<=10)&(F.col("hours")>0)).select(F.col("dbu")/F.col("hours")).collect()]
  sql=[x[0] for x in u.where(F.col("sku").contains("SQL")&F.col("sku").contains("PRO")&F.col("warehouse").isNotNull()).groupBy("warehouse").agg((F.sum("dbu")*30/LOOKBACK_DAYS).alias("m")).select("m").collect()]
  out.update(status="SOURCE_QUERIES_COMPLETED",continuous_peers=len(cont),continuous=quantiles(cont),batch_peers=len(batch),batch=quantiles(batch),sql_peers=len(sql),sql=quantiles(sql))
  try:
   q=spark.table("system.query.history").where(F.col("workspace_id").cast("string")==ws).where(F.col("start_time")>=F.date_sub(F.current_timestamp(),LOOKBACK_DAYS)).where(F.col("end_time").isNotNull())
   events=q.select(F.col("start_time").alias("t"),F.lit(1).alias("d")).unionByName(q.select(F.col("end_time").alias("t"),F.lit(-1).alias("d"))).groupBy("t").agg(F.sum("d").alias("d"));win=Window.orderBy("t").rowsBetween(Window.unboundedPreceding,0);out["peak_statements"]=events.withColumn("active",F.sum("d").over(win)).agg(F.max("active")).first()[0]
   if "executed_by" in q.columns:out["ten_users_validated"]=(q.groupBy("start_time").agg(F.countDistinct("executed_by").alias("users")).agg(F.max("users")).first()[0] or 0)>=10
  except Exception as e:out["status"]+=" | CONCURRENCY_UNAVAILABLE"
  display(u.groupBy("sku","origin").agg(F.countDistinct("resource").alias("resources"),F.countDistinct("warehouse").alias("warehouses"),F.sum("dbu").alias("dbu")).orderBy(F.desc("dbu")))
 except Exception as e:out["status"]+=" | "+str(e)[:180]
 return out
cal=calibrate(PRIMARY_WORKSPACE_ID);display(spark.createDataFrame([(str(cal),)],"summary string"))

------
%md
## 5. Recommendations, component estimates, S3 sensitivity, and monthly roll-up

------ cell 6 ---

active=FORM_STATE["intake"];decisions={c.component_id:recommend(c) for c in active.components};sql_component=Component(active.use_case_id+"-SQL","Daily and on-demand reporting","SQL_REPORTING",1,"DAILY_PLUS_ON_DEMAND");sql_decision=recommend(sql_component)
decision_rows=[]
for c in active.components+[sql_component]:
 d=sql_decision if c is sql_component else decisions[c.component_id];decision_rows.append((c.component_id,c.name,c.workload_type,c.pattern,d["selected"],d["alternative"],d["score"],d["confidence"],d["status"],d["eligible"],d["rate"],d["model"],d.get("streaming_decision","NOT_APPLICABLE")))
compute_recommendation_df=spark.createDataFrame(decision_rows,"component_id string, component_name string, workload_type string, execution_pattern string, recommended_compute string, alternative_compute string, score int, compute_confidence string, status string, eligible_options array<string>, selected_rate double, cost_model string, streaming_decision string");display(compute_recommendation_df)
component_rows=[]
for sc in SCENARIOS:
 for c in active.components:
  d=decisions[c.component_id];dbu=usd=events=runtime=None;basis="NOT_ESTIMATED";peers=0
  if c.workload_type=="CONTINUOUS_STREAMING" and d["selected"]=="JOBS_SERVERLESS":
   peers=cal["continuous_peers"];hist=cal["continuous"][{"LOW":0,"EXPECTED":1,"HIGH":2,"STRESS":2}[sc]]
   if hist is not None and peers>=3:dbu=hist*c.quantity;basis="HISTORICALLY_CALIBRATED"
   else:dbu=CONTINUOUS_HOURS_MONTH*CONTINUOUS_DBU_H[sc]*c.quantity;basis="JOBS_SERVERLESS_PRICING_PROXY_PENDING_STREAMING_CLASSIFICATION" if d.get("streaming_decision")=="UNCONFIRMED" else "ARCHITECTURE_PLANNING_FLOOR_730_HOURS_INTENSITY_VARIES"
  elif c.workload_type=="EVENT_DRIVEN_BATCH" and d["selected"]=="JOBS_SERVERLESS":
   peers=cal["batch_peers"];events=c.confirmed_events_month if c.confirmed_events_month is not None else EVENTS[sc];runtime={"LOW":c.runtime_low,"EXPECTED":c.runtime_expected,"HIGH":c.runtime_high,"STRESS":c.runtime_high}[sc] or 0;hist=cal["batch"][{"LOW":0,"EXPECTED":1,"HIGH":2,"STRESS":2}[sc]]
   if events==0:dbu=0;basis="NO_EVENT_MODELED"
   elif hist is not None and peers>=3:dbu=hist*runtime*events*c.quantity;basis="HISTORICALLY_CALIBRATED_SENSITIVITY"
   else:dbu=BATCH_DBU_H*runtime*events*c.quantity;basis="ARCHITECTURE_PLANNING_FLOOR_SENSITIVITY"
  if dbu is not None and d["rate"] is not None:usd=dbu*d["rate"]
  if c.workload_type=="EVENT_DRIVEN_BATCH":
   rate_confidence,frequency_confidence,cc=backfill_confidence(basis,peers,c.confirmed_events_month is not None)
  else:
   rate_confidence="HIGH" if basis.startswith("HISTORICALLY") and peers>=5 else ("MEDIUM" if basis.startswith("HISTORICALLY") and peers>=3 else "LOW")
   frequency_confidence="NOT_APPLICABLE"
   cc=rate_confidence if basis!="NO_EVENT_MODELED" else "LOW"
  component_rows.append((sc,c.component_id,c.name,c.workload_type,d["selected"],d["alternative"],events,runtime,peers,dbu,d["rate"],usd,basis,d["confidence"],rate_confidence,frequency_confidence,cc,c.evidence))
component_estimate_df=spark.createDataFrame(component_rows,"scenario string, component_id string, component_name string, workload_type string, recommended_compute string, alternative_compute string, modeled_events double, runtime_hours double, historical_peer_count int, estimated_dbu double, rate double, estimated_usd double, estimation_basis string, compute_confidence string, dbu_consumption_confidence string, event_frequency_confidence string, consumption_confidence string, evidence string");display(component_estimate_df)

daily={"LOW":active.daily_low,"EXPECTED":active.daily_expected,"HIGH":active.daily_high,"STRESS":active.daily_high};s3a={"LOW":(1,1.5,256),"EXPECTED":(3,2,128),"HIGH":(12,3,64),"STRESS":(12,4,32)};s3=[];roll=[]
for sc in SCENARIOS:
 new=daily[sc]*active.planning_days;rm,amp,obj=s3a[sc];retained=new*rm*amp;objects=max(1,int(retained*1024/obj));s3cost=retained*AWS_RATES["S3_GB_MONTH"]+objects/1000*(AWS_RATES["S3_PUT_LIST_1000"]+AWS_RATES["S3_GET_1000"]);s3.append((sc,new,rm,amp,obj,retained,objects,s3cost,"EXCLUDED_UNTIL_CONFIRMED"))
 cr=[x.asDict() for x in component_estimate_df.where(F.col("scenario")==sc).collect()];missing=[x["component_id"] for x in cr if x["estimated_usd"] is None];processing=None if missing else sum(x["estimated_usd"] for x in cr)
 users=max(1,int(math.ceil(active.concurrent_users*SQL_CONCURRENCY_FACTOR[sc])));bands=max(1,int(math.ceil(users/SQL_USERS_PER_CAPACITY_BAND)));sh=cal["sql"][{"LOW":0,"EXPECTED":1,"HIGH":2,"STRESS":2}[sc]]
 if sql_decision["selected"]!="SQL_PRO" or sql_decision["rate"] is None:sql_dbu=sql_usd=None;sql_basis="SELECTED_OPTION_COST_INPUTS_REQUIRED"
 elif sh is not None and cal["sql_peers"]>=3 and cal["ten_users_validated"] is True:sql_dbu=sh;sql_usd=sh*sql_decision["rate"];sql_basis="HISTORICALLY_CALIBRATED_CONCURRENCY_VALIDATED"
 else:sql_dbu=SQL_ACTIVE_HOURS_DAY[sc]*active.planning_days*SQL_BASE_DBU_H*bands;sql_usd=sql_dbu*sql_decision["rate"];sql_basis="ARCHITECTURE_CONCURRENCY_AWARE_PLANNING_FLOOR"
 ds=new*AWS_RATES["DATASYNC_GB"];total=None if processing is None or sql_usd is None else processing+sql_usd+ds;fallback=any("PLANNING_FLOOR" in x["estimation_basis"] for x in cr) or "PLANNING_FLOOR" in sql_basis;status="PARTIAL_NOT_READY" if total is None else ("TECHNICAL_PLANNING_ESTIMATE" if fallback else "HISTORICALLY_CALIBRATED_DRAFT")
 roll.append((sc,processing,users,bands,sql_dbu,sql_decision["rate"],sql_usd,new,ds,None,total,status,sql_basis,missing))
s3_df=spark.createDataFrame(s3,"scenario string, new_gb_month double, assumed_retention_months double, assumed_layer_amplification double, assumed_object_mb double, retained_gb double, objects long, s3_sensitivity_usd double, treatment string");display(s3_df)
result_df=spark.createDataFrame(roll,"scenario string, processing_usd double, modeled_concurrent_users int, sql_capacity_bands int, sql_dbu double, sql_rate double, sql_usd double, datasync_gb double, datasync_usd double, s3_usd double, recurring_monthly_total_usd double, estimate_status string, sql_basis string, missing_components array<string>");display(result_df)

# DBUs first, then applicable rate and charge. DBUs remain separated by priced service.
scenario_consumption_rows=[]
for sc in SCENARIOS:
 component_sc=[x.asDict() for x in component_estimate_df.where(F.col("scenario")==sc).collect()]
 jobs_dbu=sum((x["estimated_dbu"] or 0.0) for x in component_sc if x["recommended_compute"]=="JOBS_SERVERLESS")
 jobs_usd=sum((x["estimated_usd"] or 0.0) for x in component_sc if x["recommended_compute"]=="JOBS_SERVERLESS")
 rr=[x.asDict() for x in result_df.where(F.col("scenario")==sc).collect()][0]
 total_databricks_dbu=None if rr["sql_dbu"] is None else jobs_dbu+rr["sql_dbu"]
 databricks_usd=None if rr["sql_usd"] is None else jobs_usd+rr["sql_usd"]
 scenario_consumption_rows.append((sc,jobs_dbu,GOVERNED_RATES["JOBS_SERVERLESS"],jobs_usd,rr["sql_dbu"],rr["sql_rate"],rr["sql_usd"],total_databricks_dbu,databricks_usd,rr["datasync_gb"],AWS_RATES["DATASYNC_GB"],rr["datasync_usd"],rr["recurring_monthly_total_usd"]))
scenario_consumption_df=spark.createDataFrame(scenario_consumption_rows,"scenario string, jobs_serverless_dbu double, jobs_serverless_rate double, jobs_serverless_usd double, sql_pro_dbu double, sql_pro_rate double, sql_pro_usd double, total_databricks_dbu double, total_databricks_usd double, datasync_gb double, datasync_rate double, datasync_usd double, recurring_monthly_total_usd double")
display(scenario_consumption_df)


--------
%md
## 6. Complete monthly result and publication contracts

---- cell 7 ----

r={x["scenario"]:x.asDict() for x in result_df.collect()};e=r["EXPECTED"]
s3_by_scenario={x["scenario"]:x.asDict() for x in s3_df.collect()}
expected_components=[x.asDict() for x in component_estimate_df.where(F.col("scenario")=="EXPECTED").collect()]
expected_continuous=sum((x["estimated_usd"] or 0.0) for x in expected_components if x["workload_type"]=="CONTINUOUS_STREAMING")
expected_sql=e["sql_usd"] or 0.0
expected_total=e["recurring_monthly_total_usd"]
planning_floor_cost=expected_continuous+expected_sql if any("PLANNING_FLOOR" in x["estimation_basis"] for x in expected_components) or "PLANNING_FLOOR" in e["sql_basis"] else 0.0
planning_floor_share=None if not expected_total else planning_floor_cost/expected_total
overall_confidence="LOW" if planning_floor_share is None or planning_floor_share>=0.50 else ("MEDIUM" if planning_floor_share>=0.20 else "HIGH")
concurrency_validation="VALIDATED" if cal["ten_users_validated"] is True else "INCONCLUSIVE_NOT_VALIDATED"
expected_consumption=scenario_consumption_df.where(F.col("scenario")=="EXPECTED").first().asDict()


print("="*132);print("DCP SMART ESTIMATION CALCULATOR V3.8 | GOVERNED MONTHLY PLANNING ESTIMATE");print("="*132)
print(f"Use case: {active.use_case_id} | {active.use_case_name}")
print("INCLUSION SUMMARY")
print("  Recurring AWS DataSync: INCLUDED in recurring monthly total")
print("  S3 storage and requests: EXCLUDED from recurring monthly total; planning sensitivity shown separately")
print("  Potential initial 5 TB DataSync transfer: EXCLUDED and shown separately")
continuous_decision=next((decisions[c.component_id] for c in active.components if c.workload_type=="CONTINUOUS_STREAMING"),{})
print("CONTINUOUS STREAMING DECISION")
print(f"  Classification: {continuous_decision.get('streaming_decision','UNCONFIRMED')}")
print(f"  Current cost basis: {continuous_decision.get('selected')} | Status: {continuous_decision.get('status')}")
print("  True continuous low-latency: SERVERLESS_PIPELINE; applicable pipeline rate must be resolved.")
print("  Incremental AvailableNow-style: JOBS_SERVERLESS with governed Jobs rate.")
print("  Custom runtime/networking: CLASSIC_JOBS_COMPUTE; include DBU plus AWS infrastructure cost.")
print("Current recurring monthly planning midpoint:","NOT ESTIMATED" if expected_total is None else f"${expected_total:,.2f}")
print("This midpoint uses Jobs Serverless as a provisional pricing proxy for the unconfirmed continuous-streaming architecture.")
print(f"Overall estimate confidence: {overall_confidence}")
if planning_floor_share is not None: print(f"Reason: {planning_floor_share:.1%} of the expected total is driven by components using architecture planning assumptions.")
print("CONFIDENCE LEGEND")
print("  Compute confidence: confidence that the recommended service matches the workload pattern.")
print("  Price-rate confidence: confidence in the governed dollar rate applied to the billing unit.")
print("  DBU-consumption confidence: confidence in the estimated DBU amount for the workload.")
print("  Event-frequency confidence: confidence in the modeled number of backfill events.")
print("  Overall estimate confidence is capped by dominant consumption assumptions, not by price-rate confidence.")
print("ESTIMATE EXPLANATION NOTES")
print("  Continuous streaming expected DBUs: 730 average operating hours/month x 1.0 planning DBU/hour = 730 DBUs.")
print("  The 730 hours come from continuous 24x7 operation across an average month. Until streaming mode and latency are confirmed, the 1.0 DBU/hour value and Jobs Serverless rate are a visible pricing proxy, not a final architecture commitment.")
print(f"  SQL Pro expected DBUs: 1 active SQL hour/day x {active.planning_days} working days x {SQL_BASE_DBU_H:g} DBUs/hour x 1 planning concurrency band = 88 DBUs.")
print(f"  The expected SQL band represents {active.concurrent_users} concurrent users as a planning assumption; it is not a guaranteed SQL Pro capacity rating.")
print("  Dashboard availability is 8 hours/day, but expected active SQL compute is modeled as 1 hour/day, not 8 hours/day.")
print("  Backfill expected DBUs: modeled events x runtime/event x historically calibrated or planning DBUs/hour; event frequency remains a sensitivity unless confirmed.")
print("  Total Databricks DBUs are shown for visibility, but Jobs Serverless and SQL Pro are priced separately because their DBU rates differ.")
print("  Recurring DataSync is measured in GB, not DBUs. S3 and the potential initial transfer remain separate as stated in the inclusion summary.")
print("EXPECTED CONSUMPTION FIRST")
print(f"  Jobs Serverless: {expected_consumption['jobs_serverless_dbu']:,.2f} DBUs")
print(f"  SQL Pro: {expected_consumption['sql_pro_dbu']:,.2f} DBUs")
print(f"  Total Databricks consumption: {expected_consumption['total_databricks_dbu']:,.2f} DBUs across separately priced services")
print(f"  AWS DataSync: {expected_consumption['datasync_gb']:,.0f} GB")
print("EXPECTED CHARGES BY APPLICABLE RATE")
print(f"  Jobs Serverless: {expected_consumption['jobs_serverless_dbu']:,.2f} DBUs x ${expected_consumption['jobs_serverless_rate']:.4f}/DBU = ${expected_consumption['jobs_serverless_usd']:,.2f}")
print(f"  SQL Pro: {expected_consumption['sql_pro_dbu']:,.2f} DBUs x ${expected_consumption['sql_pro_rate']:.4f}/DBU = ${expected_consumption['sql_pro_usd']:,.2f}")
print(f"  Databricks subtotal: ${expected_consumption['total_databricks_usd']:,.2f}")
print(f"  AWS DataSync: {expected_consumption['datasync_gb']:,.0f} GB x ${expected_consumption['datasync_rate']:.4f}/GB = ${expected_consumption['datasync_usd']:,.2f}")
print(f"  Expected recurring monthly total: ${expected_consumption['recurring_monthly_total_usd']:,.2f}")
print("  S3: EXCLUDED; separate sensitivity only")
print("  Initial DataSync transfer: EXCLUDED; separate one-time sensitivity")

print("Continuous operation is 730 hours/month in every scenario; DBU intensity varies.");print("-"*132)
for sc in SCENARIOS:
 print(f"{sc} MONTHLY BREAKDOWN")
 for x in [z.asDict() for z in component_estimate_df.where(F.col("scenario")==sc).orderBy("component_id").collect()]:
  charge="NOT ESTIMATED" if x["estimated_usd"] is None else f"${x['estimated_usd']:,.2f}";dbu="N/A" if x["estimated_dbu"] is None else f"{x['estimated_dbu']:,.2f} DBUs";detail=f"730 hours x {CONTINUOUS_DBU_H[sc]:g} DBU/hour" if x["workload_type"]=="CONTINUOUS_STREAMING" else (f"{x['modeled_events']:g} event(s) x {x['runtime_hours']:g} hour(s)" if x["workload_type"]=="EVENT_DRIVEN_BATCH" else "")
  print(f"  {x['component_id']} | {x['component_name']} | Compute={x['recommended_compute']} | Alternative={x['alternative_compute']}")
  print(f"    Basis={x['estimation_basis']} | {detail} | {dbu} x ${x['rate']:,.4f}/DBU = {charge}")
  price_rate_confidence="HIGH" if x["rate"] is not None else "MISSING"
  print(f"    Compute confidence={x['compute_confidence']} | Price-rate confidence={price_rate_confidence} | DBU-consumption confidence={x['dbu_consumption_confidence']} | Event-frequency confidence={x['event_frequency_confidence']} | Overall consumption confidence={x['consumption_confidence']}")
  if x["workload_type"]=="EVENT_DRIVEN_BATCH" and x["modeled_events"]==0: print("    Interpretation=No backfill event modeled; this is not a forecast of zero backfill cost.")
 rr=r[sc];sql_charge="NOT ESTIMATED" if rr["sql_usd"] is None else f"${rr['sql_usd']:,.2f}";total="NOT ESTIMATED" if rr["recurring_monthly_total_usd"] is None else f"${rr['recurring_monthly_total_usd']:,.2f}"
 print(f"  {active.use_case_id}-SQL | Daily and on-demand reporting | Compute={sql_decision['selected']} | Alternative={sql_decision['alternative']}")
 print(f"    Basis={rr['sql_basis']} | Modeled concurrency={rr['modeled_concurrent_users']} users | Planning capacity bands={rr['sql_capacity_bands']}")
 print(f"    Planning assumption={SQL_USERS_PER_CAPACITY_BAND} concurrent users per capacity band; not a validated SQL Pro capacity guarantee")
 print(f"    Active SQL={SQL_ACTIVE_HOURS_DAY[sc]:g} hour(s)/day x {active.planning_days} days x {SQL_BASE_DBU_H:g} DBUs/hour x {rr['sql_capacity_bands']} band(s) = {rr['sql_dbu']:,.2f} DBUs = {sql_charge}")
 sql_price_rate_confidence="HIGH" if rr["sql_rate"] is not None else "MISSING"
 print(f"    Compute confidence={sql_decision['confidence']} | Price-rate confidence={sql_price_rate_confidence} | DBU-consumption confidence={'MEDIUM' if rr['sql_basis'].startswith('HISTORICALLY') else 'LOW'} | Historical concurrency={concurrency_validation}")
 print(f"  AWS DataSync recurring | INCLUDED | {rr['datasync_gb']:,.0f} GB x ${AWS_RATES['DATASYNC_GB']:.4f}/GB = ${rr['datasync_usd']:,.2f}")
 print(f"  S3 sensitivity | EXCLUDED | ${s3_by_scenario[sc]['s3_sensitivity_usd']:,.2f} under unconfirmed planning assumptions")
 print(f"  RECURRING MONTHLY TOTAL | {total} | {rr['estimate_status']}");print("-"*132)
print(f"PLANNING RANGE: Low ${r['LOW']['recurring_monthly_total_usd']:,.2f} | Expected ${r['EXPECTED']['recurring_monthly_total_usd']:,.2f} | High ${r['HIGH']['recurring_monthly_total_usd']:,.2f}")
print(f"STRESS SENSITIVITY: ${r['STRESS']['recurring_monthly_total_usd']:,.2f}; operating sensitivity, not a forecast")
print(f"Potential initial DataSync: {active.current_gb:,.0f} GB x ${AWS_RATES['DATASYNC_GB']:.4f}/GB = ${active.current_gb*AWS_RATES['DATASYNC_GB']:,.2f}, separate and excluded")
print(f"SQL requirement={active.concurrent_users} expected concurrent users | historical validation={concurrency_validation} | peak active statements={cal['peak_statements']}")
print("Concurrent statements are not treated as proof of distinct concurrent users.");print(f"Primary workspace: {PRIMARY_WORKSPACE_ID}");print("="*132)
publication_result_df=(result_df.join(scenario_consumption_df.select("scenario","jobs_serverless_dbu","jobs_serverless_rate","jobs_serverless_usd","total_databricks_dbu","total_databricks_usd"),"scenario","left").withColumn("calculator_version",F.lit(VERSION)).withColumn("use_case_id",F.lit(active.use_case_id)).withColumn("primary_workspace_id",F.lit(PRIMARY_WORKSPACE_ID)).withColumn("overall_estimate_confidence",F.lit(overall_confidence)).withColumn("datasync_included",F.lit(True)).withColumn("s3_included",F.lit(False)).withColumn("initial_transfer_included",F.lit(False)).withColumn("initial_transfer_usd_separate",F.lit(active.current_gb*AWS_RATES["DATASYNC_GB"])).withColumn("s3_treatment",F.lit("SEPARATE_SENSITIVITY_EXCLUDED")).withColumn("historical_concurrency_validation",F.lit(concurrency_validation)).withColumn("continuous_dbu_explanation",F.lit("730 average operating hours/month x scenario DBU intensity; expected intensity is 1.0 DBU/hour")).withColumn("sql_dbu_explanation",F.lit("Expected: 1 active SQL hour/day x 22 working days x 4 DBUs/hour x 1 planning concurrency band = 88 DBUs")).withColumn("rate_application_note",F.lit("Jobs Serverless and SQL Pro DBUs are priced separately; DataSync is priced per GB")).withColumn("price_rate_confidence",F.lit("HIGH for governed Jobs Serverless and SQL Pro rates")).withColumn("confidence_semantics",F.lit("Price-rate confidence is separate from DBU-consumption and event-frequency confidence")).withColumn("continuous_streaming_classification",F.lit(continuous_decision.get("streaming_decision","UNCONFIRMED"))).withColumn("continuous_compute_status",F.lit(continuous_decision.get("status"))).withColumn("continuous_pricing_treatment",F.lit("Jobs Serverless pricing proxy until streaming mode and latency are confirmed")));display(publication_result_df)
learning_log_df=component_estimate_df.withColumn("calculator_version",F.lit(VERSION)).withColumn("actual_dbu",F.lit(None).cast("double")).withColumn("actual_usd",F.lit(None).cast("double")).withColumn("variance_pct",F.lit(None).cast("double"));display(learning_log_df)
challenger_df=spark.createDataFrame([("WORK",False,"DISABLED_SEPARATE_EXECUTION"),("GENIE",False,"DISABLED_INDEPENDENT_ONLY"),("KNN_MLFLOW",False,"DISABLED_INDEPENDENT_ONLY")],"challenger string, enabled boolean, status string");display(challenger_df)

  
