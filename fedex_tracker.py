import os
import json
import time
import requests
from typing import List, Dict, Any, Optional
import gspread
from oauth2client.service_account import ServiceAccountCredentials


class FedExClient:
    def __init__(self, client_id: str, client_secret: str, sandbox: bool = False):
        self.client_id = client_id.strip() if client_id else ""
        self.client_secret = client_secret.strip() if client_secret else ""
        base_domain = "apis-sandbox.fedex.com" if sandbox else "apis.fedex.com"
        self.auth_url = f"https://{base_domain}/oauth/token"
        self.track_url = f"https://{base_domain}/track/v1/trackingnumbers"
        self.token: Optional[str] = None
        self.token_expiry: float = 0

    def get_token(self) -> str:
        if self.token and time.time() < (self.token_expiry - 60):
            return self.token

        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        payload = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret
        }

        try:
            response = requests.post(self.auth_url, data=payload, headers=headers, timeout=20)
            response.raise_for_status()
            data = response.json()
            self.token = data["access_token"]
            self.token_expiry = time.time() + data.get("expires_in", 3600)
            return self.token
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"Falha na autenticação com a FedEx: {e}")

    def track_batch(self, tracking_numbers: List[str]) -> List[Dict[str, Any]]:
        token = self.get_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-locale": "pt_BR"
        }

        tracking_info_list = [
            {"trackingNumberInfo": {"trackingNumber": str(awb).strip()}}
            for awb in tracking_numbers
        ]

        payload = {
            "includeDetailedScans": True,
            "trackingInfo": tracking_info_list
        }

        try:
            response = requests.post(self.track_url, json=payload, headers=headers, timeout=30)
            response.raise_for_status()
            res_json = response.json()
            return res_json.get("output", {}).get("completeTrackResults", [])
        except requests.exceptions.RequestException as e:
            print(f"Erro ao consultar lote FedEx: {e}")
            return []


class FedExParser:
    CLEARANCE_CODES = ["CD", "DE"]
    CLEARANCE_KEYWORDS = [
        "clearance", "customs", "alfandega", "alfândega", "aduaneira",
        "liberação", "liberacao", "fiscalização", "retido",
        "formal", "receita federal", "import", "release"
    ]

    @staticmethod
    def extract_latest_date_from_track(track_obj: Dict[str, Any]) -> str:
        """Coleta todas as datas disponíveis no objeto para determinar o ano/mês mais recente."""
        dates = []
        for d in track_obj.get("dateAndTimes", []):
            if d.get("dateTime"):
                dates.append(str(d.get("dateTime")))
        for s in track_obj.get("scanEvents", []):
            if s.get("date"):
                dates.append(str(s.get("date")))
        return max(dates) if dates else ""

    @classmethod
    def parse_best_shipment(cls, awb: str, candidate_results: List[Dict[str, Any]]) -> Dict[str, str]:
        """Dentre todos os históricos retornados para uma mesma AWB (reciclada), escolhe o mais novo."""
        all_tracks = []
        for comp in candidate_results:
            for t in comp.get("trackResults", []):
                if "error" not in t:
                    all_tracks.append(t)

        if not all_tracks:
            return {
                "AWB": awb,
                "Status": "NÃO ENCONTRADO",
                "Aduana_Alerta": "NÃO",
                "Local_Atual": "N/A",
                "Data_Entrega": "N/A",
                "Detalhe": "Sem eventos para esta AWB"
            }

        # Seleciona o histórico com a data mais avançada (ex: 2026 supera 2023)
        track_selected = max(all_tracks, key=cls.extract_latest_date_from_track)

        # Status Geral
        status_detail = track_selected.get("latestStatusDetail", {})
        code = status_detail.get("code", "")
        desc = status_detail.get("description", "Em Trânsito")
        status_resumido = "EM TRÂNSITO"

        if code == "DL":
            status_resumido = "ENTREGUE"
        elif code in ["DE", "CD"]:
            status_resumido = "RETIDO / EXCEÇÃO"

        # Local Atual (ordena scanEvents cronologicamente de forma decrescente)
        scan_events = track_selected.get("scanEvents", [])
        local_atual = "Não identificado"
        last_event_desc = ""

        if scan_events:
            sorted_scans = sorted(scan_events, key=lambda x: str(x.get("date", "")), reverse=True)
            latest_scan = sorted_scans[0]
            last_event_desc = latest_scan.get("eventDescription", "")

            loc = latest_scan.get("scanLocation", {})
            parts = [loc.get("city"), loc.get("stateOrProvinceCode"), loc.get("countryCode")]
            local_atual = ", ".join([p for p in parts if p]) or "Não identificado"

        # Alerta Alfandegário
        aduana_alerta = "NÃO"
        text_to_check = f"{desc} {code} {last_event_desc}".lower()
        if code in cls.CLEARANCE_CODES or any(kw in text_to_check for kw in cls.CLEARANCE_KEYWORDS):
            if "release" in text_to_check or "liberado" in text_to_check:
                aduana_alerta = "✅ LIBERADO NA ALFÂNDEGA"
            else:
                aduana_alerta = "⚠️ AGUARDANDO LIBERAÇÃO / ADUANA"

        # Previsão ou Data de Entrega
        dates = track_selected.get("dateAndTimes", [])
        actual_del = next((d.get("dateTime") for d in dates if d.get("type") == "ACTUAL_DELIVERY"), None)
        est_del = next((d.get("dateTime") for d in dates if d.get("type") in ["ESTIMATED_DELIVERY", "COMMITMENT"]), None)

        if actual_del:
            data_entrega = f"Entregue em {str(actual_del)[:10]}"
        elif est_del:
            data_entrega = f"Prevista: {str(est_del)[:10]}"
        else:
            data_entrega = "N/A"

        detalhe_final = desc
        if last_event_desc and last_event_desc.lower() not in desc.lower():
            detalhe_final = f"{desc} ({last_event_desc})"

        return {
            "AWB": awb,
            "Status": status_resumido,
            "Aduana_Alerta": aduana_alerta,
            "Local_Atual": local_atual,
            "Data_Entrega": data_entrega,
            "Detalhe": detalhe_final
        }


def chunk_list(data: List[Any], chunk_size: int = 30):
    for i in range(0, len(data), chunk_size):
        yield data[i:i + chunk_size]


def sync_google_sheets():
    gcp_key = os.getenv("GCP_SA_KEY")
    sheet_id = os.getenv("GSHEET_ID")
    client_id = os.getenv("FEDEX_CLIENT_ID")
    client_secret = os.getenv("FEDEX_CLIENT_SECRET")
    is_sandbox = os.getenv("FEDEX_ENV", "production").lower() == "sandbox"

    if not gcp_key or not sheet_id:
        raise ValueError("Secrets GCP_SA_KEY ou GSHEET_ID não configurados.")

    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds_dict = json.loads(gcp_key)
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    gc = gspread.authorize(creds)

    sh = gc.open_by_key(sheet_id)
    ws = sh.worksheet("FedEx")

    all_data = ws.get_all_values()
    if len(all_data) <= 1:
        print("Planilha sem registros para consulta.")
        return

    headers = all_data[0]
    rows = all_data[1:]

    col_awb_idx = 0
    col_status_idx = 1 if len(headers) > 1 else -1

    awbs_to_track = []
    row_mapping = {}

    for row_num, row in enumerate(rows, start=2):
        awb = row[col_awb_idx].strip() if len(row) > col_awb_idx else ""
        current_status = row[col_status_idx].strip() if col_status_idx != -1 and len(row) > col_status_idx else ""

        if awb and current_status != "ENTREGUE":
            awbs_to_track.append(awb)
            row_mapping.setdefault(awb, []).append(row_num)

    unique_awbs = list(set(awbs_to_track))
    print(f"Total de remessas pendentes para atualização: {len(unique_awbs)}")

    if not unique_awbs:
        print("Nenhuma remessa pendente de atualização. Processo concluído.")
        return

    client = FedExClient(client_id=client_id, client_secret=client_secret, sandbox=is_sandbox)
    
    # Dicionário para agrupar TODOS os retornos por AWB: awb -> [completeTrackResult1, completeTrackResult2, ...]
    raw_grouped_results: Dict[str, List[Any]] = {a: [] for a in unique_awbs}

    for chunk in chunk_list(unique_awbs, chunk_size=30):
        raw_items = client.track_batch(chunk)
        for item in raw_items:
            ret_awb = str(item.get("trackingNumber", "")).strip()
            if ret_awb in raw_grouped_results:
                raw_grouped_results[ret_awb].append(item)

    # Processa cada AWB selecionando estritamente a remessa mais nova
    parsed_results = {}
    for awb, candidates in raw_grouped_results.items():
        parsed_results[awb] = FedExParser.parse_best_shipment(awb, candidates)

    now_str = time.strftime("%d/%m/%Y %H:%M")
    cells_to_update = []

    for awb, rows_list in row_mapping.items():
        data = parsed_results.get(awb, {
            "Status": "FALHA CONSULTA",
            "Aduana_Alerta": "-",
            "Local_Atual": "-",
            "Data_Entrega": "-",
            "Detalhe": "Não retornou dados"
        })

        for r_num in rows_list:
            cells_to_update.append(gspread.Cell(r_num, 2, data["Status"]))
            cells_to_update.append(gspread.Cell(r_num, 3, data["Aduana_Alerta"]))
            cells_to_update.append(gspread.Cell(r_num, 4, data["Local_Atual"]))
            cells_to_update.append(gspread.Cell(r_num, 5, data["Data_Entrega"]))
            cells_to_update.append(gspread.Cell(r_num, 6, now_str))
            cells_to_update.append(gspread.Cell(r_num, 7, data["Detalhe"]))

    if cells_to_update:
        ws.update_cells(cells_to_update)
        print(f"Planilha sincronizada com sucesso! {len(cells_to_update)} células atualizadas.")


if __name__ == "__main__":
    sync_google_sheets()
