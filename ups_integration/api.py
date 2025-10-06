import json
import uuid
import frappe
import requests
from frappe.utils import get_datetime, cint, today, getdate, add_to_date, get_link_to_form, now
from frappe.integrations.utils import create_request_log


def make_api_request(
    method,
    url,
    headers=None,
    json_data=None,
    params=None,
    files=None,
    success_codes=(200, 201),
    service_name=None,
    log_args=None
):
    """
    Wrapper around requests with consistent error handling & logging.
    Returns: (result, error)
    """
    
    error, result, request_doc = None, {}, None
    try:
        response = requests.request(
            method=method,
            url=url,
            headers=headers,
            json=json_data,
            files=files,
            params=params,
        )

        if response.status_code in success_codes:
            result = response.json()
        else:
            if response.content:
                # content is bytes, so decode safely
                try:
                    error = response.content.decode("utf-8", errors="replace")
                except Exception:
                    error = str(response.content)
            else:
                error = f"status_code: {response.status_code}",
            frappe.log_error(
                title=f"MFC API failed.",
                message=f"Status: {response.status_code} \n\nUrl: {url} \n\nError: {error}",
            )

    except Exception:
        error = frappe.get_traceback()

    if log_args or files:
        request_doc = create_request_log(
            log_args,
            service_name=service_name,
            error=error,
            output=result,
            is_remote_request=1,
            url=url,
            status=error and "Failed" or "Completed"
        )

    if files and request_doc:
        modified_date = get_datetime()
        for fieldname, filetuple in files.items():
            filename, filedata, mimetype = filetuple

            frappe.get_doc(
                {
                    "doctype": "File",
                    "file_name": f"{modified_date}-{filename}",
                    "content": filedata.read() if hasattr(filedata, "read") else filedata,
                    "is_private": True,
                    "attached_to_doctype": "Integration Request",
                    "attached_to_name": request_doc.name,
                }
            ).insert()

    return result, error


class UPSClient:
    """
    get access token and cache for expiry time.
    
    """
    
    def __init__(self) -> None:
        self.ACCESS_TOKEN_KEY = 'ups_api_access_token'
        self.settings = frappe.get_doc("Parcel Service Settings")

        self.__initialize_auth()

    def __initialize_auth(self):
        """Initialize and setup authentication details"""
        self.access_token = frappe.cache().get_value(self.ACCESS_TOKEN_KEY)
        if not self.access_token:
            self.access_token = self.get_auth_token()
        self.headers = {"Authorization": f"Bearer {self.access_token}"}
    
    def get_auth_token(self):
        # return self
       
        try:
            response = requests.request(
                url=self.settings.ups_oauth_url,
                method="POST",
                data = {
                    "grant_type": "client_credentials"
                },
                headers = {
                    "Content-Type": "application/x-www-form-urlencoded",
                    "x-merchant-id": self.settings.ups_account_number
                },
                auth=(self.settings.client_id,self.settings.client_secret)
            )
            data = frappe._dict(response.json())
            frappe.cache().set_value(
                self.ACCESS_TOKEN_KEY,
                data.access_token,
                expires_in_sec=cint(data.expires_in )- 300,
            )
            return data.access_token
        except Exception as e:
            frappe.log_error(
                title="UPS OAuth token connection failed.",
                message=frappe.get_traceback(),
            )


@frappe.whitelist()
def get_ups_tracking_data(delivery_note):
    client = UPSClient()
    headers = client.headers
    headers.update({
        "transId": str(uuid.uuid4()),
        "transactionSrc": client.settings.ups_app_name,
    })
    
    params = {
        "locale": client.settings.locale,
    }

    reference_number = delivery_note 
    
    delivery_note_doc = frappe.get_doc("Delivery Note", delivery_note)
    tracking_method = ''
    
    if delivery_note_doc.tracking_number == None:
        ENDPOINT_URL = f"{client.settings.ups_server_url}{client.settings.track_by_reference_number_url}{reference_number}"
        params.update({
            "fromPickUpDate": getdate(add_to_date(today(), days=-int(client.settings.check_no_of_days_api))).strftime("%Y%m%d"),
            "toPickUpDate": getdate(today()).strftime("%Y%m%d"),
            "refNumType": client.settings.ref_number_type
        })
        tracking_method = "By Reference"

    elif delivery_note_doc.tracking_number != None:
        print("Fetching Data Using Tracking Number.")
        tracking_id = delivery_note_doc.tracking_number
        ENDPOINT_URL = f"{client.settings.ups_server_url}{client.settings.track_by_inquiry_number_url}{tracking_id}"
        params.update({
            "returnSignature": "false",
            "returnMilestones": "false",
            "returnPOD": "false"
        })
        tracking_method = "By Tracking ID"
        
    result, error = make_api_request("GET", ENDPOINT_URL, client.headers, success_codes=(200,), json_data={}, params=params, service_name="UPS Tracking Details", log_args={"url": ENDPOINT_URL})

    set_data_in_delivery_note(reference_number, result, error, tracking_method)


def set_data_in_delivery_note(delivery_note, result, error, tracking_method):
    dn_doc = frappe.get_doc("Delivery Note", delivery_note)
    
    # Code-Status Mapping
    setting_doc = frappe.get_doc("Parcel Service Settings")
    code_status_map = {}
    for status in setting_doc.status_code_description:
        code_status_map.update({status.status_code: status.jammy_description})
    
    # First check for API error if occurs then create error log and give its link in message and stop api call
    if error:
        log = frappe.log_error(
            title="UPS Get Details API Key Error",
            message=json.dumps(error),
        )
        dn_doc.custom_tracking = "ERPNext Exception" 
        dn_doc.save()   
        frappe.msgprint('Error occur while fetching data from API, See Details: {0}'.format(get_link_to_form("Error Log", log.name)), indicator = "red")
        return

    # Check Traking Data is available or not if not then set status and stop api call
    if "warnings" in result['trackResponse']['shipment'][0].keys():
        dn_doc.custom_tracking = "DN Not Found In UPS"
        dn_doc.save()
        frappe.msgprint(result['trackResponse']['shipment'][0]['warnings'][0]['message'], indicator = "red")
        return

    # If not error then find tracking id and code 
    packages = result['trackResponse']['shipment'][0]['package']
    if len(packages) > 0:
        if tracking_method == "By Reference":
            parent_ups_tracking_id = packages[0]['trackingNumber']
            parent_ups_tracking_code = packages[0]['currentStatus']['code']
            parent_jammy_status_code = ''
            
            for status in setting_doc.status_code_description:
                if status.status_code == cint(parent_ups_tracking_code):
                    parent_jammy_status_code = status.jammy_description

            # Setting data in Parent Fields + Child Table
            dn_doc = frappe.get_doc("Delivery Note", delivery_note)
            dn_doc.tracking_number = parent_ups_tracking_id
            dn_doc.custom_tracking_code = parent_ups_tracking_code
            dn_doc.custom_tracking = parent_jammy_status_code
            dn_doc.custom_last_api_call = now()

            for package in packages:
                dn_doc.append("custom_tracking_details", {
                    "tracking_id" : package['trackingNumber'],
                    "status_code" : package['currentStatus']['code'],
                    "status_description" : code_status_map[cint(package['currentStatus']['code'])],
                })
            dn_doc.save(ignore_permissions=True)
            frappe.msgprint("Tracking Details Saved!", alert = True)
        
        elif tracking_method == "By Tracking ID":
            print("Continue Fetching Data")
            dn_doc = frappe.get_doc("Delivery Note", delivery_note)
            package_activities = packages[0]['activity']
            if len(package_activities) > 0:
                updated_status_code = package_activities[0]['status']['statusCode'] 
                upadated_status_description = code_status_map[cint(updated_status_code)]
                dn_doc.custom_tracking_code = updated_status_code
                dn_doc.custom_tracking = upadated_status_description
                dn_doc.custom_last_api_call = now()
                dn_doc.save(ignore_permissions=True)
                frappe.msgprint("Latest Tracking Details Of {0} Saved!".format(dn_doc.tracking_number), alert = True)

@frappe.whitelist()
def update_dn_by_schedular():
    settings = frappe.get_doc("Parcel Service Settings")
    start = today()
    end = add_to_date(today(), days=-int(settings.check_no_of_days_scheduler))
    
    eligible_dns = frappe.db.sql("""
        SELECT dn.name
        FROM `tabDelivery Note` dn
        WHERE dn.posting_date BETWEEN '{0}' AND '{1}'
        AND dn.ship_via LIKE "%UPS%"
        AND dn.docstatus = 1
        AND (dn.custom_tracking = "Processing" OR dn.custom_tracking = "In Transit" OR dn.custom_tracking = "DN Not Found In UPS" OR dn.custom_tracking IS NULL);
    """.format(end, start)
    ,as_dict=1)

    if len(eligible_dns) > 0:
        for dn in eligible_dns:
            delivery_note = dn['name']
            get_ups_tracking_data(delivery_note)

@frappe.whitelist()
def fillup_status_code_data():
    code_details = [
		{
			"status_code": "0",
			"ups_description": "Status Not Available",
			"jammy_description": "Not Found"
		},
		{
			"status_code": "3",
			"ups_description": "Shipment Ready for UPS",
			"jammy_description": "Processing"
		},
		{
			"status_code": "5",
			"ups_description": "On the Way",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "6",
			"ups_description": "Out for Delivery",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "7",
			"ups_description": "Shipment Information Voided",
			"jammy_description": "Cancelled"
		},
		{
			"status_code": "10",
			"ups_description": "On the Way",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "11",
			"ups_description": "Delivered",
			"jammy_description": "Delivered"
		},
		{
			"status_code": "12",
			"ups_description": "Clearance in Progress",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "13",
			"ups_description": "Exception",
			"jammy_description": "Exception"
		},
		{
			"status_code": "14",
			"ups_description": "Clearance Completed",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "16",
			"ups_description": "In Warehouse",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "17",
			"ups_description": "Held for Customer Pickup",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "18",
			"ups_description": "Delivery Change Requested: Hold for Pickup",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "19",
			"ups_description": "Held for Future Delivery",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "20",
			"ups_description": "Held for Future Delivery Requested",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "21",
			"ups_description": "Out for Delivery",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "22",
			"ups_description": "First Attempt Made",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "23",
			"ups_description": "Second Delivery Attempted",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "24",
			"ups_description": "Final Attempt Made",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "25",
			"ups_description": "On the Way",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "26",
			"ups_description": "Delivered by Local Post Office",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "27",
			"ups_description": "Delivery Address Change Requested",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "28",
			"ups_description": "Delivery Address Changed",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "29",
			"ups_description": "Exception: Action Required",
			"jammy_description": "Exception"
		},
		{
			"status_code": "30",
			"ups_description": "Local Post Office Exception",
			"jammy_description": "Exception"
		},
		{
			"status_code": "32",
			"ups_description": "Adverse Weather May Cause Delay",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "33",
			"ups_description": "Return to Sender Requested",
			"jammy_description": "Exception"
		},
		{
			"status_code": "34",
			"ups_description": "Returned to Sender",
			"jammy_description": "Exception"
		},
		{
			"status_code": "35",
			"ups_description": "Returning to Sender",
			"jammy_description": "Exception"
		},
		{
			"status_code": "36",
			"ups_description": "Returning to Sender: In Transit",
			"jammy_description": "Exception"
		},
		{
			"status_code": "37",
			"ups_description": "Out for Delivery",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "38",
			"ups_description": "Picked Up by UPS",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "39",
			"ups_description": "On the Way",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "40",
			"ups_description": "Ready for Customer Pickup",
			"jammy_description": "Processing"
		},
		{
			"status_code": "41",
			"ups_description": "Service Upgrade Requested",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "42",
			"ups_description": "Service Upgraded",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "43",
			"ups_description": "Voided Pickup",
			"jammy_description": "Cancelled"
		},
		{
			"status_code": "44",
			"ups_description": "On the Way to UPS",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "45",
			"ups_description": "On the Way to UPS",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "46",
			"ups_description": "Delay",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "47",
			"ups_description": "On the Way",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "48",
			"ups_description": "Delay",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "49",
			"ups_description": "Delay: Action Required",
			"jammy_description": "Exception"
		},
		{
			"status_code": "50",
			"ups_description": "Address Information Required",
			"jammy_description": "Exception"
		},
		{
			"status_code": "51",
			"ups_description": "Delay: Emergency Situation or Severe Weather",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "52",
			"ups_description": "Delay: Severe Weather",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "53",
			"ups_description": "Delay: Severe Weather",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "54",
			"ups_description": "Delivery Change Requested",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "55",
			"ups_description": "Rescheduled Delivery",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "56",
			"ups_description": "Service Upgrade Requested",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "57",
			"ups_description": "On the Way to a Local UPS Access Point™",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "58",
			"ups_description": "Clearance Information Required",
			"jammy_description": "Exception"
		},
		{
			"status_code": "59",
			"ups_description": "Damage Reported",
			"jammy_description": "Exception"
		},
		{
			"status_code": "60",
			"ups_description": "Delivery Attempted",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "61",
			"ups_description": "Delivery Attempted: Adult Signature Required",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "62",
			"ups_description": "Delivery Attempted: Funds Required",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "63",
			"ups_description": "Delivery Change Completed",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "64",
			"ups_description": "Delivery Refused",
			"jammy_description": "Exception"
		},
		{
			"status_code": "65",
			"ups_description": "Pickup Attempted",
			"jammy_description": "Processing"
		},
		{
			"status_code": "66",
			"ups_description": "Post Office Delivery Attempted",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "67",
			"ups_description": "Returned to Sender by Post Office",
			"jammy_description": "Exception"
		},
		{
			"status_code": "68",
			"ups_description": "Sent to Lost and Found",
			"jammy_description": "Exception"
		},
		{
			"status_code": "69",
			"ups_description": "Unable to Deliver",
			"jammy_description": "Exception"
		},
		{
			"status_code": "70",
			"ups_description": "Package not at UPS Access Point™ yet",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "71",
			"ups_description": "Preparing for Delivery",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "72",
			"ups_description": "Loaded on Delivery Vehicle",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "73",
			"ups_description": "In Transit to UPS Delivery Partner",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "74",
			"ups_description": "UPS Delivery Partner has Shipment",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "75",
			"ups_description": "Scheduled for Delivery",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "76",
			"ups_description": "UPS Delivery Partner Exception",
			"jammy_description": "Exception"
		},
		{
			"status_code": "77",
			"ups_description": "Scheduled for Pickup Today",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "78",
			"ups_description": "Your Driver is Arriving Soon!",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "79",
			"ups_description": "Order Processed: In Transit to UPS",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "80",
			"ups_description": "Order Processed: Ready for UPS",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "81",
			"ups_description": "Returned - Damage Reported",
			"jammy_description": "Exception"
		},
		{
			"status_code": "82",
			"ups_description": "Delivery Instructions Received",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "83",
			"ups_description": "Held",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "84",
			"ups_description": "Cleared",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "85",
			"ups_description": "Held for COD Payment",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "86",
			"ups_description": "Delay",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "87",
			"ups_description": "On the Way",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "88",
			"ups_description": "Test",
			"jammy_description": "Processing"
		},
		{
			"status_code": "89",
			"ups_description": "Out for Delivery",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "90",
			"ups_description": "Delay",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "91",
			"ups_description": "Out for Delivery",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "92",
			"ups_description": "Customs Clearance in Progress",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "93",
			"ups_description": "Premier Recovery In Progress",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "94",
			"ups_description": "Premier Recovery Completed",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "95",
			"ups_description": "Additional Attempt Requested",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "96",
			"ups_description": "Address Change Confirmed",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "97",
			"ups_description": "Address Change Requested",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "98",
			"ups_description": "Deliver to Original Address Requested",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "99",
			"ups_description": "Deliver to Original Address Confirmed",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "100",
			"ups_description": "Hold at UPS Access Point™ Confirmed",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "101",
			"ups_description": "Hold at UPS Access Point™ Requested",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "102",
			"ups_description": "Hold for Courier Requested",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "103",
			"ups_description": "Hold for Courier Confirmed",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "104",
			"ups_description": "Hold for Instructions Confirmed",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "105",
			"ups_description": "Hold for Instructions Requested",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "106",
			"ups_description": "Hold for Pickup Confirmed",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "107",
			"ups_description": "Hold for Pickup Requested",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "108",
			"ups_description": "Hold for Pickup Today Confirmed",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "109",
			"ups_description": "Hold for Pickup Today Requested",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "110",
			"ups_description": "Refrigeration Confirmed",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "111",
			"ups_description": "Refrigeration Requested",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "112",
			"ups_description": "Re-Ice Confirmed",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "113",
			"ups_description": "Re-Ice Requested",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "114",
			"ups_description": "Request Canceled",
			"jammy_description": "Cancelled"
		},
		{
			"status_code": "115",
			"ups_description": "Reschedule Delivery Confirmed",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "116",
			"ups_description": "Reschedule Delivery Requested",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "117",
			"ups_description": "Return by Saturday Confirmed",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "118",
			"ups_description": "Return to Sender Confirmed",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "119",
			"ups_description": "Return to Sender Requested",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "120",
			"ups_description": "Saturday Delivery Confirmed",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "121",
			"ups_description": "Saturday Delivery Requested",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "122",
			"ups_description": "Upgrade Confirmed",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "123",
			"ups_description": "Pending Release From Non-UPS Broker",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "124",
			"ups_description": "Clearance Information Needed",
			"jammy_description": "Exception"
		},
		{
			"status_code": "125",
			"ups_description": "Clearance Information Needed",
			"jammy_description": "Exception"
		},
		{
			"status_code": "126",
			"ups_description": "Pending Government Agency Release",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "127",
			"ups_description": "Investigation Closed",
			"jammy_description": "Processed"
		},
		{
			"status_code": "128",
			"ups_description": "Investigation Canceled",
			"jammy_description": "Processed"
		},
		{
			"status_code": "129",
			"ups_description": "Investigation Opened",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "130",
			"ups_description": "Claim in Progress",
			"jammy_description": "Processed"
		},
		{
			"status_code": "131",
			"ups_description": "Final Pickup Attempted",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "132",
			"ups_description": "Airport Security Delay",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "133",
			"ups_description": "Return Label Left With Customer",
			"jammy_description": "Processing"
		},
		{
			"status_code": "134",
			"ups_description": "Cleared Import Customs",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "135",
			"ups_description": "Second Pickup Attempted",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "136",
			"ups_description": "Delivery Rescheduled for Saturday",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "137",
			"ups_description": "Transferred to UPS Delivery Partner",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "138",
			"ups_description": "Awaiting Scheduled Departure",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "139",
			"ups_description": "Security Access Required",
			"jammy_description": "Exception"
		},
		{
			"status_code": "140",
			"ups_description": "Claim Paid - Claim Payment Has Been Processed.",
			"jammy_description": "Processed"
		},
		{
			"status_code": "141",
			"ups_description": "Incomplete Documentation Received",
			"jammy_description": "Exception"
		},
		{
			"status_code": "142",
			"ups_description": "Claim Voided",
			"jammy_description": "Processed"
		},
		{
			"status_code": "143",
			"ups_description": "Delivered to Agent",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "144",
			"ups_description": "Delivered to Post Office for Pickup",
			"jammy_description": "Delivered"
		},
		{
			"status_code": "145",
			"ups_description": "Delivery Attempted",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "146",
			"ups_description": "Out for Delivery",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "147",
			"ups_description": "Seized by Law Enforcement, No Longer in UPS possession",
			"jammy_description": "Cancelled"
		},
		{
			"status_code": "148",
			"ups_description": "Package Information Unavailable",
			"jammy_description": "Exception"
		},
		{
			"status_code": "149",
			"ups_description": "Prohibited Contents, Package Destroyed No Longer in UPS possession",
			"jammy_description": "Exception"
		},
		{
			"status_code": "153",
			"ups_description": "Updated Delivery Time",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "154",
			"ups_description": "Updated Delivery Date",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "155",
			"ups_description": "Delivery Photo",
			"jammy_description": "Delivered"
		},
		{
			"status_code": "156",
			"ups_description": "Commercial Inside Release",
			"jammy_description": "Delivered"
		},
		{
			"status_code": "157",
			"ups_description": "Shipment Ready for UPS",
			"jammy_description": "Processing"
		},
		{
			"status_code": "158",
			"ups_description": "On the Way",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "159",
			"ups_description": "On the Way",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "160",
			"ups_description": "We Have Your Package",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "161",
			"ups_description": "Delivered to UPS Access Point",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "162",
			"ups_description": "Out for Delivery",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "163",
			"ups_description": "Package Information Unavailable",
			"jammy_description": "Exception"
		},
		{
			"status_code": "164",
			"ups_description": "On the Way",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "165",
			"ups_description": "On the Way",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "166",
			"ups_description": "Shipment Ready for Roadie",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "167",
			"ups_description": "Dropped off at UPS Store by Customer",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "168",
			"ups_description": "Dropped off at Retail Location by Customer",
			"jammy_description": "In Transit"
		},
		{
			"status_code": "169",
			"ups_description": "Dropped off at a UPS Access Point by Customer",
			"jammy_description": "In Transit"
		},
	]
    setting_doc = frappe.get_doc("Parcel Service Settings")
    print("Filling Status Code In Parcel Service Settings")
    if setting_doc.status_code_description == []:
        for code in code_details:
            setting_doc.append('status_code_description', {
                'status_code' : code['status_code'],
                'jammy_description' : code['jammy_description'],
                'ups_description' : code['ups_description']
            })
        setting_doc.save(ignore_permissions=True)