import streamlit as st
import json
import requests
import time
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
import pytz
import gspread
from google.oauth2.service_account import Credentials

# ==========================================
# 1. CẤU HÌNH HỆ THỐNG
# ==========================================
VN_TZ = pytz.timezone('Asia/Ho_Chi_Minh')
REGISTER_URL = "https://api.17track.net/track/v2.4/register"
TRACK_INFO_URL = "https://api.17track.net/track/v2.4/gettrackinfo"
BATCH_SIZE = 40 

# Bản đồ Carrier Code
CARRIER_MAP = {
    "USPS": 21051,
    "DHL": 10001,       # DHL Express
    "DHL_ECOMM": 14031  # DHL eCommerce
}

def detect_carrier(track_num):
    """Tự động nhận diện carrier. Ưu tiên USPS và DHL eCommerce."""
    tn = str(track_num).strip().upper()
    # USPS thường có 22 số hoặc bắt đầu bằng 9
    if tn.startswith('9') or len(tn) == 22:
        return CARRIER_MAP["USPS"]
    # Mã e-commerce của DHL thường có tiền tố JD, GM, hoặc các dải số đặc thù
    if tn.startswith(('JD', 'JJD', 'GM', '7', '1', '420')):
        return CARRIER_MAP["DHL_ECOMM"] # Đổi thành 14031 thay vì 10001
    return 0 # Auto-detect là an toàn nhất cho các mã lạ

# Load Secrets
try:
    TRACK17_API_KEY = st.secrets["TRACK17_API_KEY"]
    SPREADSHEET_ID = st.secrets["SPREADSHEET_ID"]
    GCP_JSON_STR = st.secrets["GCP_JSON"]
    EMAIL_SENDER = st.secrets["EMAIL_SENDER"]
    EMAIL_APP_PASSWORD = st.secrets["EMAIL_APP_PASSWORD"]
except Exception:
    st.error("❌ Thiếu cấu hình Secrets!")
    st.stop()

# ==========================================
# 2. CÁC HÀM TIỆN ÍCH
# ==========================================
def get_sheet_connection():
    creds_dict = json.loads(GCP_JSON_STR)
    creds = Credentials.from_service_account_info(creds_dict, scopes=["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"])
    client = gspread.authorize(creds)
    return client.open_by_key(SPREADSHEET_ID).worksheet("Data")

def to_vn_time(iso_str):
    if not iso_str: return ""
    try:
        dt = datetime.fromisoformat(iso_str.replace('Z', '+00:00'))
        return dt.astimezone(VN_TZ).strftime("%Y-%m-%d %H:%M")
    except: return ""

def calculate_sla(label_at, transit_at):
    if not label_at: return 0
    try:
        fmt = "%Y-%m-%d %H:%M"
        t1 = VN_TZ.localize(datetime.strptime(label_at[:16], fmt))
        t2 = VN_TZ.localize(datetime.strptime(transit_at[:16], fmt)) if transit_at else datetime.now(VN_TZ)
        return int((t2 - t1).total_seconds() / 3600)
    except: return 0

def send_critical_report(receiver, total_up, new_transit, sla72_count):
    """Chỉ gửi mail khi có đơn > 72h"""
    msg = MIMEMultipart()
    msg['From'] = EMAIL_SENDER
    msg['To'] = receiver
    msg['Subject'] = f"⚠️ [CẢNH BÁO SLA >72H] {datetime.now(VN_TZ).strftime('%H:%M %d/%m')}"
    
    html = f"""
    <h3 style="color: red;">Phát hiện đơn hàng tồn kho nghiêm trọng (>72h)</h3>
    <p>Thời gian quét: <b>{datetime.now(VN_TZ).strftime('%d/%m/%Y %H:%M')}</b></p>
    <hr>
    <ul>
        <li>Tổng số đơn vừa cập nhật: <b>{total_up}</b></li>
        <li>Đơn mới chuyển sang InTransit: <b>{new_transit}</b></li>
        <li>🚨 Số đơn vi phạm SLA > 72h: <b style="color:red; font-size: 20px;">{sla72_count} đơn</b></li>
    </ul>
    <p>Vui lòng kiểm tra lại hệ thống vận hành ngay lập tức!</p>
    """
    msg.attach(MIMEText(html, 'html'))
    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(EMAIL_SENDER, EMAIL_APP_PASSWORD)
            server.send_message(msg)
        return True
    except: return False

# ==========================================
# 3. GIAO DIỆN & LOGIC
# ==========================================
st.set_page_config(page_title="Azura Multi-Carrier", page_icon="⚡")
st.title("⚡ Azura SLA Control (USPS & DHL)")

try:
    sheet = get_sheet_connection()
    raw_data = sheet.get_all_values(value_render_option='FORMATTED_VALUE')
    headers = raw_data[0]
    data_rows = raw_data[1:]
    cols = {h: i for i, h in enumerate(headers)}
    
    reg_list = [i+2 for i, r in enumerate(data_rows) if r[cols['Register_Track']].lower() != 'done' and r[cols['Tracking_Number']]]
    
    track_list = []
    for i, r in enumerate(data_rows):
        stt = r[cols['17Track_Status']]
        if r[cols['Register_Track']].lower() == 'done' and stt not in ["InTransit", "Delivered", "Returned"]:
            track_list.append({"row": i+2, "num": r[cols['Tracking_Number']], "old_stt": stt, "old_label": r[cols['Label_Created_At']]})

    m1, m2, m3 = st.columns(3)
    m1.metric("Tổng dòng", len(data_rows))
    m2.metric("Chưa Register", len(reg_list))
    m3.metric("Chờ InTransit", len(track_list))

except Exception as e:
    st.error(f"Lỗi: {e}")
    st.stop()

st.divider()
mail_to = st.text_input("Email nhận cảnh báo (>72h):", value=st.secrets.get("EMAIL_RECEIVER", ""))
btn_reg, btn_track = st.columns(2)

# --- NÚT 1: REGISTER ---
if btn_reg.button(f"🚀 Register {len(reg_list)} đơn mới", use_container_width=True):
    if not reg_list: st.info("Không có mã mới.")
    else:
        bar = st.progress(0)
        for i in range(0, len(reg_list), BATCH_SIZE):
            rows = reg_list[i:i+BATCH_SIZE]
            # Tự nhận diện carrier cho từng mã trong lô
            batch_api = [{"number": data_rows[r-2][cols['Tracking_Number']], "carrier": detect_carrier(data_rows[r-2][cols['Tracking_Number']])} for r in rows]
            requests.post(REGISTER_URL, json=batch_api, headers={"17token": TRACK17_API_KEY})
            
            updates = [{'range': f'C{r}:D{r}', 'values': [["done", "Pending"]]} for r in rows]
            sheet.batch_update(updates)
            bar.progress(min((i + BATCH_SIZE) / len(reg_list), 1.0))
        st.success("✅ Register hoàn tất!")
        st.rerun()

# --- NÚT 2: UPDATE & BÁO CÁO ---
if btn_track.button(f"📡 Update & Check SLA >72h", use_container_width=True, type="primary"):
    if not track_list: st.info("Không có đơn cần cập nhật.")
    else:
        new_in_transit = 0
        total_up = 0
        bar = st.progress(0)
        now_vn = datetime.now(VN_TZ).strftime("%Y-%m-%d %H:%M")

        for i in range(0, len(track_list), BATCH_SIZE):
            batch = track_list[i:i+BATCH_SIZE]
            
            # CHỈ truyền tracking number, bỏ carrier để 17Track tự dùng carrier đã register
            batch_api = [{"number": x['num']} for x in batch]
            
            res = requests.post(TRACK_INFO_URL, json=batch_api, headers={"17token": TRACK17_API_KEY}).json()
            accepted = res.get("data", {}).get("accepted", [])
            rejected = res.get("data", {}).get("rejected", [])
            
            if rejected:
                print("Rejected items:", rejected) # Log ra terminal để debug nếu cần
            
            updates = []
            for item in accepted:
                num = item.get("number")
                info = item.get("track_info") or {}
                new_stt = (info.get("latest_status") or {}).get("status", "Pending")
                
                orig = next(x for x in batch if x['num'] == num)
                if new_stt == "InTransit" and orig['old_stt'] != "InTransit":
                    new_in_transit += 1
                
                l_vn, t_vn = "", ""
                all_events = []
                
                # Gom toàn bộ event từ tất cả các providers (DHL và Last-mile carrier như USPS)
                providers = info.get("tracking", {}).get("providers", []) or []
                for p in providers:
                    all_events.extend(p.get("events", []))
                
                # Sắp xếp và trích xuất thời gian
                for ev in sorted(all_events, key=lambda x: x.get("time_utc", "")):
                    desc = ev.get("description", "").lower()
                    t_str = to_vn_time(ev.get("time_utc"))
                    
                    if ("label created" in desc or "info received" in desc) and not l_vn: 
                        l_vn = t_str
                    if ("in transit" in desc or "accepted" in desc or "picked up" in desc or "arrived" in desc) and not t_vn: 
                        t_vn = t_str
                
                eff_label = l_vn if l_vn else orig['old_label']
                sla = calculate_sla(eff_label, t_vn)
                updates.append({'range': f'D{orig["row"]}:H{orig["row"]}', 'values': [[new_stt, eff_label, t_vn, sla, now_vn]]})
                total_up += 1
            
            if updates: sheet.batch_update(updates)
            bar.progress(min((i + BATCH_SIZE) / len(track_list), 1.0))

        # LOGIC GỬI EMAIL CHỈ KHI SLA > 72
        with st.spinner("📧 Kiểm tra điều kiện gửi mail..."):
            new_raw = sheet.get_all_values(value_render_option='FORMATTED_VALUE')
            s72 = 0
            for r in new_raw[1:]:
                # Chỉ tính đơn chưa chuyển phát thành công/đang đi
                if r[cols['17Track_Status']] not in ["InTransit", "Delivered", "Returned"]:
                    try:
                        if int(r[cols['SLA_Status']]) > 72: s72 += 1
                    except: pass
            
            if s72 > 0 and mail_to:
                send_critical_report(mail_to, total_up, new_in_transit, s72)
                st.warning(f"🚨 Phát hiện {s72} đơn > 72h. Đã gửi mail cảnh báo!")
            else:
                st.info("✅ Không có đơn vi phạm SLA > 72h. Không gửi mail.")
        
        st.success(f"✅ Đã cập nhật xong {total_up} mã!")
        st.rerun()
