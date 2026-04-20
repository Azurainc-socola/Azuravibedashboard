import streamlit as st
import json
import requests
import smtplib
import io
import csv
import re
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import gspread
from google.oauth2.service_account import Credentials

# ==========================================
# THIẾT LẬP GIAO DIỆN & THỜI GIAN
# ==========================================
st.set_page_config(page_title="TMZ Sync System", page_icon="🚀", layout="centered")

VN_TZ = timezone(timedelta(hours=7))
today_vn = datetime.now(VN_TZ).date()

# ĐỌC SECRETS
try:
    AZ_USER = str(st.secrets["AZURA_USER"]).strip()
    AZ_PASS = str(st.secrets["AZURA_PASS"]).strip()
    GS_ID = str(st.secrets["GOOGLE_SHEET_ID"]).strip() # Bạn nhớ thay ID Sheet mới vào Secrets
    MAIL_USER = str(st.secrets["EMAIL_USER"]).strip()
    MAIL_PASS = str(st.secrets["EMAIL_PASS"]).replace(" ", "").strip()
    GCP_JSON = str(st.secrets["GCP_SERVICE_ACCOUNT_JSON"]).strip()
except Exception as e:
    st.error(f"❌ Thiếu biến Secret: {e}")
    st.stop()

st.title("🚀 TMZ DATA SYNC (ID: 154)")

with st.form("main_form"):
    col1, col2 = st.columns(2)
    with col1:
        date_pick = st.date_input("Chọn khoảng ngày quét đơn", (today_vn, today_vn))
    with col2:
        is_mail = st.checkbox("Gửi Email báo cáo", value=True)
        mail_to = st.text_input("Gửi đến Email", placeholder="email1, email2...")
    
    run_btn = st.form_submit_button("🚀 BẮT ĐẦU ĐỒNG BỘ", use_container_width=True)

if run_btn:
    if len(date_pick) == 2:
        start_date, end_date = date_pick
    else:
        start_date = end_date = date_pick[0]

    with st.status("🔍 Đang xử lý dữ liệu...", expanded=True) as status:
        
        # 1. ĐĂNG NHẬP
        st.write("🌐 Đăng nhập Portal...")
        session = requests.Session()
        cookie_str = ""
        
        try:
            r1 = session.get("https://portal.aluffm.com/Login", timeout=15)
            match = re.search(r'name="__RequestVerificationToken"[^>]*value="([^"]+)"', r1.text)
            if not match:
                st.error("❌ Không lấy được Token đăng nhập.")
                st.stop()
            
            payload = {"UserName": AZ_USER, "Password": AZ_PASS, "__RequestVerificationToken": match.group(1), "RememberMe": "false"}
            session.post("https://portal.aluffm.com/Login", data=payload, headers={"Referer": "https://portal.aluffm.com/Login"}, allow_redirects=False)

            if '.AspNetCore.Identity.Application' in session.cookies.get_dict():
                cookie_str = "; ".join([f"{k}={v}" for k, v in session.cookies.get_dict().items()])
            else:
                st.error("❌ Đăng nhập thất bại.")
                st.stop()
        except Exception as e:
            st.error(f"❌ Lỗi kết nối: {e}")
            st.stop()

        # 2. QUÉT ĐƠN HÀNG (TẬP TRUNG CUSTOMER ID 154)
        start_str = start_date.strftime('%Y-%m-%d')
        end_str = end_date.strftime('%Y-%m-%d')
        all_data = []
        page = 1
        
        while True:
            st.write(f"⏳ Đang quét dữ liệu trang {page}...")
            params = {
                "pageSize": 50, 
                "pageNumber": page, 
                "customerId": 154,  # Fixed ID cho TMZ
                "sortOrder": "asc"
            }
            headers = {'Cookie': cookie_str, 'X-Requested-With': 'XMLHttpRequest'}
            res = requests.get("https://portal.aluffm.com/OnBehalfOrder/List", headers=headers, params=params, timeout=20)
            
            if res.status_code != 200: break
            rows = res.json().get("rows", [])
            if not rows: break

            stop_page = False
            for row in rows:
                created_at = row.get("createdAt", "")[:10]
                status_str = row.get("orderStatusString", "")
                
                # Check điều kiện dừng theo ngày
                if created_at < start_str:
                    stop_page = True
                    break

                # LOGIC LỌC MỚI: 
                # 1. Trong khoảng ngày
                # 2. Trạng thái không phải Cancel
                # 3. Lấy mọi loại shippingPartner
                if start_str <= created_at <= end_str and status_str != "Cancel":
                    designs = row.get("orderProductDesigns", [])
                    job_ids = [str(d.get("jobId")) for d in designs if d.get("jobId") is not None]
                    job_id_str = ", ".join(sorted(list(set(job_ids))))

                    all_data.append({
                        "Seller_Name": row.get("customer", ""),
                        "Tracking_Number": row.get("partnerBarcode", ""),
                        "Order_Number": row.get("customerOrder", ""),
                        "Job_ID": job_id_str,
                        "AzuraID": row.get("id", ""),
                        "Azura_Creat_At": created_at,
                        "Vendor_ship": row.get("shippingPartnerString", "") # Dữ liệu cho cột M
                    })
                        
            if stop_page: break
            page += 1
            if page > 500: break 
            
        if not all_data:
            status.update(label="Hoàn tất - Không có đơn hàng mới", state="complete")
            st.stop()

        # 3. GHI GOOGLE SHEET (CẬP NHẬT CỘT M)
        st.write(f"📂 Đang ghi {len(all_data)} đơn vào Google Sheet...")
        try:
            creds = Credentials.from_service_account_info(json.loads(GCP_JSON), scopes=["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"])
            client = gspread.authorize(creds)
            sheet = client.open_by_key(GS_ID).sheet1

            final_rows = []
            for item in all_data:
                # Tạo list 13 phần tử (Cột A đến M)
                r = [""] * 13 
                r[0] = item["Seller_Name"]       # Cột A
                r[1] = item["Tracking_Number"]   # Cột B
                r[8] = item["Order_Number"]     # Cột I
                r[9] = item["Job_ID"]           # Cột J
                r[10] = item["AzuraID"]         # Cột K
                r[11] = item["Azura_Creat_At"]  # Cột L
                r[12] = item["Vendor_ship"]     # Cột M (Index 12)
                final_rows.append(r)

            sheet.append_rows(final_rows, value_input_option="USER_ENTERED")
            success_sheet = True
        except Exception as e:
            st.error(f"❌ Lỗi ghi Sheet: {e}")
            success_sheet = False

        # 4. GỬI EMAIL
        if success_sheet and is_mail and mail_to:
            try:
                msg = MIMEMultipart()
                msg['Subject'] = f"[TMZ Sync] Báo cáo đơn hàng ({start_str})"
                msg['From'] = MAIL_USER
                msg['To'] = mail_to
                body = f"<h3>📊 Báo cáo đồng bộ TMZ (ID: 154)</h3><p>Tổng đơn đã xử lý: {len(all_data)}</p><p>🔗 <a href='https://docs.google.com/spreadsheets/d/{GS_ID}'>Mở bảng tính</a></p>"
                msg.attach(MIMEText(body, 'html'))
                
                server = smtplib.SMTP('smtp.gmail.com', 587)
                server.starttls()
                server.login(MAIL_USER, MAIL_PASS)
                server.sendmail(MAIL_USER, [x.strip() for x in mail_to.split(',')], msg.as_string())
                server.quit()
            except: pass

        status.update(label="🎉 HOÀN TẤT!", state="complete")
    
    st.success(f"Đã đồng bộ **{len(all_data)}** đơn hàng của TMZ vào hệ thống.")
