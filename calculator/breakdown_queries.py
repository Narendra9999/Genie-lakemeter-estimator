"""Engine breakdown SQL over unrounded calculator line items."""
import json

def breakdown_sql(arguments):
    return f"""SELECT li.scenario, li.engine, li.recurrence,
       SUM(CASE WHEN li.treatment = 'INCLUDED' THEN li.cost_usd END) AS priced_subtotal_usd,
       SUM(CASE WHEN li.treatment = 'PENDING' THEN 1 ELSE 0 END) AS pending_items,
       CASE WHEN SUM(CASE WHEN li.treatment = 'PENDING' THEN 1 ELSE 0 END) > 0
            THEN 'PARTIAL_PENDING_INPUTS' ELSE 'COMPLETE' END AS status
FROM __FQ__.dcp_calc_line_items({arguments}) AS li
GROUP BY li.scenario, li.engine, li.recurrence
ORDER BY li.scenario, li.engine, li.recurrence"""

def request_argument(request):
    return "p_request => '" + json.dumps(request, separators=(',', ':')).replace("'", "''") + "'"
