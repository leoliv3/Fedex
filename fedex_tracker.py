import os
import json
import time
import re
import requests
from typing import List, Dict, Any, Optional
import gspread
from oauth2client.service_account import ServiceAccountCredentials


class FedExClient:
    """Gerencia autenticação OAuth 2.0 e requisições em lote à API da FedEx."""

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

        response = requests.post(self.auth_url, data=payload, headers=headers, timeout=20)
        if response.status_code != 200:
            print(f"[ERRO AUTH FEDEX] {response.status_code} - {response.text}")
        response.raise_for_status()
        data = response.json()
        self.token = data["access_token"]
        self.token_expiry = time.time() + data.get("expires_in", 3600)
        return self.token

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

        response = requests.post(self.track_url, json=payload, headers=headers, timeout=30)
        if response.status_code != 200:
            print(f"[ERRO TRACK FEDEX] {response.status_code} - {response.text}")
        response.raise_for_status()
        res_json = response.json()
        return res_json.get("output", {}).get("completeTrackResults", [])


class FedExParser:
    CLEARANCE_KEYWORDS = [
        "clearance", "customs", "alfandega", "alfândega", "aduaneira",
        "liberação", "liberacao", "fiscalização", "retido",
        "formal", "receita federal", "campinas", "viracopos", "vcp"
    ]

    @staticmethod
    def _extract_all_dates_as_timestamp(track_obj: Dict[str, Any]) -> float:
        """Busca todas as datas possíveis no objeto (scans, entrega, despacho) e retorna a mais recente em timestamp."""
        timestamps = [0.0]

        # 1. Datas de eventos de scan
        for scan in track_obj.get("scanEvents", []):
            dt_str = scan.get("date")
            if dt_str:
                clean_str = str(dt_str)[:19].replace("T", " ")
                for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                    try:
                        timestamps.append(time.mktime(time.strptime(clean_str[:len(fmt) + 2], fmt)))
                        break
                    except Exception:
                        pass

        # 2. Datas do bloco dateAndTimes
        for dt_item in track_obj.get("dateAndTimes", []):
            dt_str = dt_item.get("dateTime")
            if dt_str:
                clean_str = str(dt_str)[:19].replace("T", " ")
                for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                    try:
                        timestamps.append(time.mktime(time.strptime(clean_str[:len(fmt) + 2], fmt)))
                        break
                    except Exception:
                        pass

        return max(timestamps)

    @classmethod
    def parse_best_track(cls, awb: str, candidate_complete_results: List[Dict[str, Any]]) -> Dict[str, str]:
        all_tracks = []
        for comp in candidate_complete_results:
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
                "Detalhe": "Sem histórico retornado"
            }

        # Seleciona o pacote com o timestamp mais recente (2026 vence qualquer histórico de 2023)
        track_selected = max(all_tracks, key=cls._extract_all_dates_as_timestamp)

        # 1. Identificar Status e Código
        status_detail = track_selected.get("latestStatusDetail", {})
        code = status_detail.get("code", "")
        desc = status_detail.get("description", "Em Trânsito")
        status_resumido = "EM TRÂNSITO"

        if code == "DL":
            status_resumido = "ENTREGUE"
        elif code in ["DE", "CD"]:
            status_resumido = "RETIDO / EXCEÇÃO"

        # 2. Localização Atual e Histórico de Scans
        scan_events = track_selected.get("scanEvents", [])
        local_atual = "Não identificado"
        last_event_desc = ""

        if scan_events:
            # Ordena por data decrescente
            sorted_scans = sorted(
                scan_events,
                key=lambda s: str(s.get("date", "")),
                reverse=True
            )
            latest_scan = sorted_scans[0]
            last_event_desc = latest_scan.get("eventDescription", "")

            loc = latest_scan.get("scanLocation", {})
            parts = [loc.get("city"), loc.get("stateOrProvinceCode"), loc.get("countryCode")]
            local_atual = ", ".join([p for p in parts if p]) or "Não identificado"

        # Se foi entregue, o local atual deve refletir a entrega no destino se disponível
        if code == "DL":
            dest_loc = track_selected.get("destinationLocation", {}).get("locationContactAndAddress", {}).get("address", {})
            dest_city = dest_loc.get("city")
            dest_state = dest_loc.get("stateOrProvinceCode")
            dest_country = dest_loc.get("countryCode", "BR")
            if dest_city:
                local_atual = f"{dest_city}, {dest_state} {dest_country}".strip()

        # 3. Alerta de Aduana / Liberação Formal
        aduana_alerta = "NÃO"
        check_text = f"{desc} {code} {last_event_desc} {local_atual}".lower()

        # Detecta retenção aduaneira (ex: Campinas / VCP com Delivery Exception)
        if "campinas" in check_text or "viracopos" in check_text or "vcp" in check_text:
            if code in ["DE", "CD"] or "exception" in check_text or "clearance" in check_text:
                aduana_alerta = "⚠️ AGUARDANDO LIBERAÇÃO / ADUANA (VCP)"
        elif code in ["CD", "DE"] or any(kw in check_text for kw in cls.CLEARANCE_KEYWORDS):
            if "release" in check_text or "liberado" in check_text:
                aduana_alerta = "✅ LIBERADO NA ALFÂNDEGA"
            else:
                aduana_alerta = "⚠️ AGUARDANDO LIBERAÇÃO / ADUANA"

        # 4. Data de Entrega ou Previsão
        dates = track_selected.get("dateAndTimes", [])
        data_entrega = "N/A"
        actual_del = next((d.get("dateTime") for d in dates if d.get("type") == "ACTUAL_DELIVERY"), None)
        est_del = next((d.get("dateTime") for d in dates if d.get("type") in ["ESTIMATED_DELIVERY", "COMMITMENT"]), None)

        if actual_del:
            data_entrega = f"Entregue em {str(actual_del)[:10]}"
        elif est_del:
            data_entrega = f"Prevista: {str(est_del)[:10]}"

        # Detalhe informativo
        detalhe_final = desc
        if last_event_desc and last_event_desc.lower() not in desc.lower():
            detalhe_final = f"{desc} - {last_event_desc}"

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
        raise ValueError("GCP_SA_KEY ou GSHEET_ID não configurados.")

    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds_dict = json.loads(gcp_key)
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    gc = gspread.authorize(creds)

    sh = gc.open_by_key(sheet_id)
    ws = sh.worksheet("FedEx")

    all_data = ws.get_all_values()
    if len(all_data) <= 1:
        print("Nenhum dado a processar.")
        return

    headers = all_data[0]
    rows = all_data[1:]

    col_awb_idx = 0
    awbs_to_track = []
    row_mapping = {}

    for row_num, row in enumerate(rows, start=2):
        awb = str(row[col_awb_idx]).strip() if len(row) > col_awb_idx else ""
        if awb:
            awbs_to_track.append(awb)
            row_mapping.setdefault(awb, []).append(row_num)

    unique_awbs = list(set(awbs_to_track))
    print(f"AWBs a consultar: {unique_awbs}")

    client = FedExClient(client_id=client_id, client_secret=client_secret, sandbox=is_sandbox)

    # Agrupa todos os resultados por AWB
    raw_grouped_results: Dict[str, List[Any]] = {a: [] for a in unique_awbs}

    for chunk in chunk_list(unique_awbs, chunk_size=30):
        raw_items = client.track_batch(chunk)
        for item in raw_items:
            ret_awb = str(item.get("trackingNumber", "")).strip()
            if ret_awb in raw_grouped_results:
                raw_grouped_results[ret_awb].append(item)

    parsed_results = {}
    for awb, candidates in raw_grouped_results.items():
        parsed_results[awb] = FedExParser.parse_best_track(awb, candidates)

    now_str = time.strftime("%d/%m/%Y %H:%M")
    cells_to_update = []

    for awb, rows_list in row_mapping.items():
        data = parsed_results.get(awb, {
            "Status": "ERRO",
            "Aduana_Alerta": "-",
            "Local_Atual": "-",
            "Data_Entrega": "-",
            "Detalhe": "Falha"
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
        print(f"Planilha sincronizada com sucesso! {len(cells_to_update)} células gravadas.")


if __name__ == "__main__":
    sync_google_sheets()
