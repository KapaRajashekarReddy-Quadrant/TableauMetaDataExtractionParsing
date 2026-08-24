import os
import re
import zipfile
import tempfile
import logging
import xml.etree.ElementTree as ET
from typing import Dict, List, Any

# ============================================================
# LOGGING
# ============================================================
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("tableau-metadata")

# ============================================================
# CONSTANTS & MAPPINGS
# ============================================================
MARK_MAP = {
    'bar': 'Bar Chart',
    'line': 'Line Chart',
    'area': 'Area Chart',
    'text': 'Text Table',
    'circle': 'Scatter Plot',
    'square': 'Heat Map',
    'pie': 'Pie Chart',
    'map': 'Map',
    'ganttbar': 'Gantt Chart',
    'shape': 'Shape Chart',
    'scatter': 'Scatter Plot',
    'multipolygon': 'Map',
    'filledmap': 'Map',
    'polygon': 'Map',
    'automatic': 'Standard Visual'
}

DATA_TYPE_MAP = {
    "integer": "integer",
    "real": "real",
    "string": "string",
    "date": "date",
    "datetime": "datetime",
    "boolean": "boolean"
}

# ============================================================
# UTILS & STRING CLEANERS
# ============================================================
def strip_ns(root: ET.Element):
    for el in root.iter():
        if "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]

def clean(val: str) -> str:
    if not val:
        return ""
    return re.sub(r'[\[\]"]', "", val).strip()

def normalize_table_name(name: str) -> str:
    name = clean(name)
    name = re.sub(r"\s*\(.*?\)", "", name)
    name = re.sub(r"[_\-]?[0-9a-fA-F]{32}", "", name)
    name = re.sub(r"\.(csv|txt|xlsx|xls|hyper|tde)", "", name, flags=re.IGNORECASE)
    name = re.sub(r'^Extract[_\s]?', '', name, flags=re.IGNORECASE)
    name = name.split("#")[0]
    name = re.sub(r"[^a-zA-Z0-9 _-]", "", name).strip()
    return name

def is_junk_table(name: str) -> bool:
    name = name.lower()
    if name.startswith("federated") or name in ["clipboard", "csv", ""]:
        return True
    return False

def clean_visual_column_name(name: str) -> str:
    if not name:
        return ""
    name = name.replace("[", "").replace("]", "")
    name = re.sub(r'^(none|sum|avg|min|max|count|attr|yr|mn|dy|qd|tdc|usr|pcto|rank):', '', name, flags=re.IGNORECASE)
    name = re.sub(r':(nk|ok|qk|sk)(:\d+)?$', '', name, flags=re.IGNORECASE)
    name = re.sub(r"\s*\(.*?\)", "", name)
    return name.strip()

# ============================================================
# STEP 1: HYPER METADATA (Physical Store)
# ============================================================
def extract_hyper_metadata(hyper_path: str) -> Dict[str, List[Dict[str, str]]]:
    tables: Dict[str, List[Dict[str, str]]] = {}
    if not hyper_path or not os.path.exists(hyper_path):
        return tables

    try:
        from tableauhyperapi import HyperProcess, Telemetry, Connection
        with HyperProcess(telemetry=Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU) as hyper:
            with Connection(hyper.endpoint, hyper_path) as conn:
                for schema in conn.catalog.get_schema_names():
                    for table in conn.catalog.get_table_names(schema):
                        table_name = normalize_table_name(str(table.name))
                        if is_junk_table(table_name):
                            continue
                        cols = []
                        try:
                            table_def = conn.catalog.get_table_definition(table)
                            for c in table_def.columns:
                                col_type = str(c.type).lower()
                                mapped_type = "string"
                                if "int" in col_type: mapped_type = "integer"
                                elif "double" in col_type or "numeric" in col_type or "float" in col_type: mapped_type = "real"
                                elif "date" in col_type: mapped_type = "date"
                                elif "bool" in col_type: mapped_type = "boolean"
                                
                                cols.append({
                                    "name": clean(str(c.name)),
                                    "dataType": mapped_type
                                })
                        except Exception:
                            pass
                        if cols:
                            tables[table_name] = cols
    except Exception as e:
        log.warning(f"Hyper extraction skipped or failed: {e}")
    return tables

# ============================================================
# STEP 2: CALCULATED FIELDS METADATA
# ============================================================
def extract_calculations(root: ET.Element) -> List[Dict[str, Any]]:
    calculations = []
    seen = set()

    for col in root.findall(".//column"):
        calc = col.find("calculation")
        caption = col.get("caption") or col.get("name")
        calc_id = col.get("name", "")
        
        if calc is not None and "formula" in calc.attrib:
            clean_name = clean(caption)
            if clean_name in seen:
                continue
            seen.add(clean_name)

            table_calc_node = col.find(".//table-calc")
            table_calc_def = None
            if table_calc_node is not None:
                table_calc_def = {k: v for k, v in table_calc_node.attrib.items()}

            calculations.append({
                "calculationId": clean(calc_id),
                "name": clean_name,
                "fieldType": "calculatedField",
                "role": col.get("role", "measure"),
                "table": None,
                "dataType": DATA_TYPE_MAP.get(col.get("datatype", "real"), col.get("datatype", "real")),
                "formula": calc.get("formula"),
                "defaultFormat": col.get("default-format"),
                "tableCalculationDefinition": table_calc_def
            })
    return calculations

# ============================================================
# STEP 3: XML METADATA & RELATIONSHIPS
# ============================================================
def extract_xml_metadata(root: ET.Element):
    xml_tables: Dict[str, List[Dict[str, str]]] = {}
    local_name_map: Dict[str, dict] = {}

    for record in root.findall(".//metadata-record[@class='column']"):
        remote_name = record.find("remote-name")
        parent_name = record.find("parent-name")
        local_name_node = record.find("local-name")
        type_node = record.find("local-type")

        if remote_name is not None and parent_name is not None:
            col = clean(remote_name.text)
            clean_tbl = normalize_table_name(parent_name.text)
            raw_type = type_node.text if type_node is not None else "string"
            datatype = DATA_TYPE_MAP.get(raw_type, raw_type)

            if not is_junk_table(clean_tbl):
                xml_tables.setdefault(clean_tbl, [])
                if not any(c["name"] == col for c in xml_tables[clean_tbl]):
                    xml_tables[clean_tbl].append({"name": col, "dataType": datatype})

                if local_name_node is not None and local_name_node.text:
                    raw_local = local_name_node.text
                    info = {"table": clean_tbl, "col": col, "dataType": datatype}
                    local_name_map[raw_local] = info
                    local_name_map[clean(raw_local)] = info

    for relation in root.findall(".//relation[@type='table']"):
        t_name = relation.get("name") or relation.get("table") or "Unknown"
        clean_tbl = normalize_table_name(t_name)
        if not is_junk_table(clean_tbl):
            xml_tables.setdefault(clean_tbl, [])
            for col in relation.findall(".//column"):
                col_name = clean(col.get("name"))
                col_type = col.get("datatype", "string")
                if col_name and not any(c["name"] == col_name for c in xml_tables[clean_tbl]):
                    xml_tables[clean_tbl].append({
                        "name": col_name,
                        "dataType": DATA_TYPE_MAP.get(col_type, col_type)
                    })

    return xml_tables, local_name_map

def extract_relationships(root: ET.Element, valid_tables: dict, local_name_map: dict):
    relationships = []
    seen = set()
    valid_names = set(valid_tables.keys())

    def add(from_t, from_c, to_t, to_c):
        from_t = normalize_table_name(from_t)
        to_t = normalize_table_name(to_t)
        if from_t not in valid_names or to_t not in valid_names or from_t == to_t:
            return
        key = (from_t, from_c, to_t, to_c)
        if key in seen:
            return
        seen.add(key)
        relationships.append({
            "fromTable": from_t,
            "fromColumn": from_c,
            "toTable": to_t,
            "toColumn": to_c,
            "relationshipType": "Many-to-One"
        })

    for rel in root.findall(".//clause[@type='join']") + root.findall(".//relationship"):
        expr = rel.find("expression") or rel
        ops = []
        for sub in expr.iter("expression"):
            op = sub.get("op")
            if op and (op.startswith("[") or op in local_name_map):
                ops.append(op)
        if len(ops) == 2:
            info1 = local_name_map.get(ops[0]) or local_name_map.get(clean(ops[0]))
            info2 = local_name_map.get(ops[1]) or local_name_map.get(clean(ops[1]))
            if info1 and info2:
                add(info1['table'], info1['col'], info2['table'], info2['col'])

    return relationships

# ============================================================
# STEP 4: VISUAL METADATA (Full Extraction)
# ============================================================
def extract_visual_metadata(root: ET.Element, column_to_table: Dict[str, str], calculations_list: List[Dict[str, Any]]):
    worksheets_data = []
    dashboards_data = []
    calc_lookup = {c["calculationId"]: c for c in calculations_list}
    calc_name_lookup = {c["name"]: c for c in calculations_list}

    # --- A. Worksheets Extraction ---
    for ws in root.findall(".//worksheet"):
        sheet_name = ws.get('name')
        
        # Title logic
        title_run = ws.find(".//title/formatted-text/run")
        raw_title = title_run.text if (title_run is not None and title_run.text) else "<Sheet Name>"
        display_title = sheet_name if "<Sheet Name>" in raw_title else raw_title.replace("\n", " ").strip()

        # Shelves raw strings
        rows_str = ws.findtext(".//rows", default="")
        cols_str = ws.findtext(".//cols", default="")

        # Determine visual mark type
        mark_elem = ws.find(".//pane/mark")
        mark_class = mark_elem.get('class', 'automatic').lower() if mark_elem is not None else 'automatic'
        visual_type = MARK_MAP.get(mark_class, 'Bar Chart')

        # Columns, Encoded Fields, Filters, and Table Calculations
        fields = []
        encodings = []
        filters = []
        table_calcs = []
        columns_ref = []

        # 1. Dependency Analysis
        for dep in ws.findall(".//datasource-dependencies"):
            for col_inst in dep.findall("column-instance"):
                inst_name = col_inst.get("name", "")
                raw_col = col_inst.get("column", "")
                clean_col = clean_visual_column_name(raw_col) or clean_visual_column_name(inst_name)
                derivation = col_inst.get("derivation", "None")
                inst_type = col_inst.get("type", "nominal")
                
                # Calculation resolution
                calc_meta = calc_lookup.get(clean(raw_col)) or calc_name_lookup.get(clean_col)
                is_calc = calc_meta is not None
                
                field_role = "dimension" if inst_type in ["nominal", "ordinal"] else "measure"
                field_table = None if is_calc else column_to_table.get(clean_col, "Unknown")
                
                # Check shelf presence
                shelf_location = "Marks"
                if inst_name in rows_str: shelf_location = "Rows"
                elif inst_name in cols_str: shelf_location = "Columns"

                field_obj = {
                    "fieldType": "calculatedField" if is_calc else "column",
                    "role": field_role,
                    "table": field_table,
                    "column": clean_col,
                    "dataType": calc_meta["dataType"] if is_calc else "string",
                    "name": calc_meta["name"] if is_calc else clean_col,
                    "instanceName": inst_name,
                    "derivation": derivation,
                    "instanceType": inst_type,
                    "shelf": shelf_location
                }

                if is_calc:
                    field_obj["calculationId"] = calc_meta["calculationId"]
                    field_obj["formula"] = calc_meta["formula"]
                    if calc_meta.get("defaultFormat"):
                        field_obj["defaultFormat"] = calc_meta["defaultFormat"]

                fields.append(field_obj)
                columns_ref.append({
                    "table": field_table,
                    "column": field_obj["name"],
                    "fieldType": field_obj["fieldType"],
                    "calculationId": field_obj.get("calculationId")
                })

        # 2. Panes & Encodings
        for pane in ws.findall(".//pane"):
            for role_tag in ["color", "size", "tooltip", "text", "lod", "wedge-size"]:
                for el in pane.findall(f".//{role_tag}"):
                    col_ref = el.get("column", "")
                    clean_c = clean_visual_column_name(col_ref)
                    calc_m = calc_lookup.get(clean(col_ref)) or calc_name_lookup.get(clean_c)

                    enc_obj = {
                        "name": calc_m["name"] if calc_m else clean_c,
                        "role": role_tag,
                        "encoding": role_tag,
                        "fieldType": "calculatedField" if calc_m else "column",
                        "table": None if calc_m else column_to_table.get(clean_c, "Unknown"),
                        "instanceName": col_ref
                    }
                    if calc_m:
                        enc_obj["calculationId"] = calc_m["calculationId"]
                        enc_obj["dataType"] = calc_m["dataType"]
                        enc_obj["formula"] = calc_m["formula"]
                    encodings.append(enc_obj)

        # 3. Filters
        for flt in ws.findall(".//filter"):
            flt_col = flt.get("column", "")
            clean_flt = clean_visual_column_name(flt_col)
            calc_m = calc_lookup.get(clean(flt_col)) or calc_name_lookup.get(clean_flt)
            
            filters.append({
                "name": calc_m["name"] if calc_m else clean_flt,
                "column": clean_flt,
                "fieldType": "calculatedField" if calc_m else "column",
                "filterClass": flt.get("class", "categorical"),
                "instanceName": flt_col
            })

        # Fallback card type detection
        if visual_type == "Standard Visual":
            if len(cols_str) == 0 and len(rows_str) == 0 and any(e["role"] == "text" for e in encodings):
                visual_type = "Card"
            else:
                visual_type = "Bar Chart"

        worksheets_data.append({
            "name": sheet_name,
            "visualType": visual_type,
            "title": {
                "text": raw_title,
                "displayText": display_title,
                "isDynamic": "<Sheet Name>" in raw_title,
                "source": "tableau_worksheet_title"
            },
            "columns": columns_ref,
            "fields": fields,
            "rows": [f for f in fields if f["shelf"] == "Rows"],
            "columnsShelf": [f for f in fields if f["shelf"] == "Columns"],
            "encodings": encodings,
            "filters": filters,
            "tableCalculations": table_calcs
        })

    # --- B. Dashboards & Geometry Normalization ---
    for db in root.findall(".//dashboard"):
        db_name = db.get("name", "Dashboard")
        size_node = db.find(".//size")
        cw = int(size_node.get("maxwidth", 1000)) if size_node is not None else 1000
        ch = int(size_node.get("maxheight", 800)) if size_node is not None else 800

        ws_list = []
        visuals = []

        for zone in db.findall(".//zone[@name]"):
            z_name = zone.get("name")
            ws_list.append(z_name)

            raw_x = float(zone.get("x", 0))
            raw_y = float(zone.get("y", 0))
            raw_w = float(zone.get("w", 0))
            raw_h = float(zone.get("h", 0))

            visuals.append({
                "name": z_name,
                "x": raw_x,
                "y": raw_y,
                "width": raw_w,
                "height": raw_h,
                "pixel_layout": {
                    "pixel_x": round((raw_x / 100000.0) * cw, 2),
                    "pixel_y": round((raw_y / 100000.0) * ch, 2),
                    "pixel_width": round((raw_w / 100000.0) * cw, 2),
                    "pixel_height": round((raw_h / 100000.0) * ch, 2)
                }
            })

        dashboards_data.append({
            "dashboardName": db_name,
            "worksheets": ws_list,
            "canvas": {"width": cw, "height": ch},
            "visuals": visuals,
            "coordinateSystem": "tableau_0_100000"
        })

    return worksheets_data, dashboards_data

# ============================================================
# MASTER ENTRY POINT
# ============================================================
def extract_metadata_from_twbx(twbx_path: str):
    log.info(f"Extracting Tableau file: {twbx_path}")
    with tempfile.TemporaryDirectory() as tmp:
        with zipfile.ZipFile(twbx_path, "r") as z:
            z.extractall(tmp)

        twb = hyper = None
        for root_dir, _, files in os.walk(tmp):
            for f in files:
                if f.endswith(".twb"): twb = os.path.join(root_dir, f)
                elif f.endswith(".hyper"): hyper = os.path.join(root_dir, f)

        if not twb:
            raise ValueError("Invalid package: .twb not found inside archive")

        tree = ET.parse(twb)
        root = tree.getroot()
        strip_ns(root)

        # 1. Calculations
        calculations = extract_calculations(root)

        # 2. Tables & Physical Schema
        xml_tables, local_name_map = extract_xml_metadata(root)
        hyper_tables = extract_hyper_metadata(hyper) if hyper else {}

        final_tables = {}
        # Merge XML + Hyper
        for t, cols in xml_tables.items():
            if not is_junk_table(t):
                final_tables[t] = cols
        for t, cols in hyper_tables.items():
            if not is_junk_table(t) and t not in final_tables:
                final_tables[t] = cols

        # 3. Relationships
        relationships = extract_relationships(root, final_tables, local_name_map)

        # 4. Inverted Column-to-Table Map
        column_to_table = {}
        for tbl, cols in final_tables.items():
            for c in cols:
                column_to_table[c["name"]] = tbl

        # 5. Worksheets & Dashboards
        worksheets, dashboards = extract_visual_metadata(root, column_to_table, calculations)

        return {
            "tables": final_tables,
            "relationships": relationships,
            "calculations": calculations,
            "worksheets": worksheets,
            "dashboards": dashboards
        }