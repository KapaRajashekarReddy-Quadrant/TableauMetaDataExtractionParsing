import os
import re
import time
import logging
import tempfile
import requests
import pandas as pd
import json

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from azure.storage.blob import BlobServiceClient
from dotenv import load_dotenv

# Import the complete updated extractor
from extractor import extract_metadata_from_twbx

from pydantic import BaseModel
from typing import List, Optional

# ============================================================
# ENV + CONFIG
# ============================================================
load_dotenv()

POWERBI_API = "https://api.powerbi.com/v1.0/myorg"

TENANT_ID = os.getenv("TENANT_ID")
CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")
TEMPLATE_WORKSPACE_ID = os.getenv("TEMPLATE_WORKSPACE_ID")
TEMPLATE_REPORT_ID = os.getenv("TEMPLATE_REPORT_ID")

AZURE_STORAGE_CONNECTION_STRING = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
TWBX_CONTAINER = os.getenv("TWBX_CONTAINER")
CSV_CONTAINER = os.getenv("CSV_CONTAINER")
METADATA_CONTAINER = os.getenv("METADATA_CONTAINER", "metadata-output")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("tableau-pbi-migrator")

app = FastAPI(title="Tableau → Power BI Semantic Builder & Metadata Parser")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================================
# MODELS
# ============================================================
class Relationship(BaseModel):
    fromTable: str
    fromColumn: str
    toTable: str
    toColumn: str
    relationshipType: str

class SemanticModelRequest(BaseModel):
    folder_name: str
    target_workspace_id: str
    relationships: List[Relationship]
    clone_report: bool = False
    report_name: Optional[str] = "Final_Manufacturing_Report"

# ============================================================
# HELPERS
# ============================================================
def extract_second_word_table_name(filename: str) -> str:
    base = filename.split(".csv")[0]
    parts = base.split("_")
    table_name = parts[1] if len(parts) >= 2 else parts[0]
    return re.sub(r"[^a-zA-Z]", "", table_name).lower()

def get_auth_token() -> str:
    url = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"
    resp = requests.post(
        url,
        data={
            "grant_type": "client_credentials",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "scope": "https://analysis.windows.net/powerbi/api/.default",
        },
    )
    resp.raise_for_status()
    return resp.json()["access_token"]

def download_file_from_blob(folder_name: str) -> str:
    blob_service = BlobServiceClient.from_connection_string(AZURE_STORAGE_CONNECTION_STRING)
    container = blob_service.get_container_client(TWBX_CONTAINER)
    
    file_name = f"{folder_name}.twbx"
    suffix = ".twbx"
    
    if not container.get_blob_client(file_name).exists():
        file_name = f"{folder_name}.twb"
        suffix = ".twb"
        if not container.get_blob_client(file_name).exists():
             raise FileNotFoundError(f"Neither .twbx nor .twb found for folder/file: {folder_name}")

    log.info(f"Downloading Workbook Blob: {file_name}")
    data = container.download_blob(file_name).readall()
    
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp.write(data)
    tmp.close()
    return tmp.name

def upload_json_to_blob(data: dict, filename: str) -> str:
    blob_service = BlobServiceClient.from_connection_string(AZURE_STORAGE_CONNECTION_STRING)
    container_client = blob_service.get_container_client(METADATA_CONTAINER)
    if not container_client.exists():
        container_client.create_container()
        
    blob_client = container_client.get_blob_client(filename)
    json_data = json.dumps(data, indent=2, ensure_ascii=False)
    blob_client.upload_blob(json_data, overwrite=True)
    
    log.info(f"Successfully uploaded perfect metadata JSON to: {filename}")
    return blob_client.url

# ============================================================
# API: EXTRACT METADATA & VISUALS PERFECTLY
# ============================================================
@app.post("/extract-metadata")
def extract_metadata(folder_name: str):
    try:
        file_path = download_file_from_blob(folder_name)
        metadata = extract_metadata_from_twbx(file_path)
        
        if os.path.exists(file_path):
            os.remove(file_path)
        
        output_filename = f"{folder_name}_metadata.json"
        blob_url = upload_json_to_blob(metadata, output_filename)
        
        return {
            "status": "SUCCESS", 
            "metadata": metadata,
            "outputBlobUrl": blob_url
        }
    except Exception as e:
        log.exception("Metadata extraction failed")
        raise HTTPException(status_code=500, detail=str(e))

# ============================================================
# API: CREATE POWER BI PUSH SEMANTIC MODEL
# ============================================================
@app.post("/create-semantic-model")
def create_semantic_model(request: SemanticModelRequest):
    try:
        folder_name = request.folder_name
        target_workspace_id = request.target_workspace_id
        relationships = request.relationships
        final_report_name = request.report_name

        log.info(f"Starting semantic model push for report: {final_report_name}")
        token = get_auth_token()
        
        blob_service = BlobServiceClient.from_connection_string(AZURE_STORAGE_CONNECTION_STRING)
        container = blob_service.get_container_client(CSV_CONTAINER)

        blob_tables = {}
        prefix = f"{folder_name.rstrip('/')}/"

        for blob in container.list_blobs(name_starts_with=prefix):
            filename = os.path.basename(blob.name)
            if not filename.lower().endswith(".csv"):
                continue
            
            table_name = extract_second_word_table_name(filename)
            data = container.download_blob(blob.name).readall()
            blob_tables[table_name] = pd.read_csv(pd.io.common.BytesIO(data))
            log.info(f"Loaded table: {table_name}")

        if not blob_tables:
            raise Exception(f"No CSV tables found under prefix: {prefix}")

        # Build Relationships
        pbi_relationships = []
        for r in relationships:
            f_tbl = re.sub(r"[^a-zA-Z]", "", r.fromTable).lower()
            t_tbl = re.sub(r"[^a-zA-Z]", "", r.toTable).lower()
            
            if f_tbl not in blob_tables or t_tbl not in blob_tables:
                log.warning(f"Skipping relationship {f_tbl} -> {t_tbl} (table missing in CSVs)")
                continue

            pbi_relationships.append({
                "name": f"{f_tbl}_{t_tbl}",
                "fromTable": f_tbl,
                "fromColumn": r.fromColumn,
                "toTable": t_tbl,
                "toColumn": r.toColumn,
                "crossFilteringBehavior": "BothDirections",
            })

        # Dataset Payload
        dataset_payload = {
            "name": f"{final_report_name}_DS",
            "defaultMode": "Push",
            "tables": [],
            "relationships": pbi_relationships,
        }

        for table_name, df in blob_tables.items():
            columns = []
            for col in df.columns:
                col_lower = col.lower()
                if "id" in col_lower:
                    dtype, summarize = "Int64", "none"
                elif df[col].dtype == "float64":
                    dtype, summarize = "Double", "sum"
                elif df[col].dtype == "int64":
                    dtype, summarize = "Int64", "sum"
                else:
                    dtype, summarize = "String", "none"
                
                columns.append({"name": col, "dataType": dtype, "summarizeBy": summarize})
            dataset_payload["tables"].append({"name": table_name, "columns": columns})

        # Create Dataset in Power BI
        log.info(f"Creating Power BI dataset in workspace {target_workspace_id}...")
        ds_resp = requests.post(
            f"{POWERBI_API}/groups/{target_workspace_id}/datasets",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=dataset_payload,
        )
        if not ds_resp.ok:
            log.error(ds_resp.text)
        ds_resp.raise_for_status()
        dataset_id = ds_resp.json()["id"]

        # Push Rows in batches
        time.sleep(2)
        for table_name, df in blob_tables.items():
            rows = df.where(pd.notnull(df), None).to_dict(orient="records")
            for i in range(0, len(rows), 2500):
                requests.post(
                    f"{POWERBI_API}/groups/{target_workspace_id}/datasets/{dataset_id}/tables/{table_name}/rows",
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                    json={"rows": rows[i:i + 2500]},
                ).raise_for_status()

        # Clone Report (Optional)
        report_id = None
        if request.clone_report and TEMPLATE_REPORT_ID:
            log.info("Cloning report template...")
            clone_resp = requests.post(
                f"{POWERBI_API}/groups/{TEMPLATE_WORKSPACE_ID}/reports/{TEMPLATE_REPORT_ID}/Clone",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={
                    "name": final_report_name,
                    "targetWorkspaceId": target_workspace_id,
                    "targetModelId": dataset_id
                },
            )
            if clone_resp.ok:
                report_id = clone_resp.json()["id"]

        return {
            "status": "SUCCESS",
            "dataset_id": dataset_id,
            "report_id": report_id,
            "report_name": final_report_name,
            "relationships_created": len(pbi_relationships),
        }

    except Exception as e:
        log.exception("Semantic model creation failed")
        raise HTTPException(status_code=500, detail=str(e))