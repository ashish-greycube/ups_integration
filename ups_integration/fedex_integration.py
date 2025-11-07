import json
import uuid
import frappe
import requests
from frappe.utils import get_datetime, cint, today, getdate, add_to_date, get_link_to_form, now
from frappe.integrations.utils import create_request_log

class FedExIntegration:
    """ 
    get access token and cache for expiry time. 
    """

    def __init__(self) -> None:
        self.ACCESS_TOKEN_KEY = 'fedex_access_token'
        self.settings = frappe.get_doc("Parcel Service Settings")
        self.__initialize_auth()

    def __initialize_auth(self):
        """
        Initialize and setup authentication details
        """
        self.access_token = frappe.cache().get_value(self.ACCESS_TOKEN_KEY)
        if not self.access_token:
            self.access_token = self.get_auth_token()
        self.headers = {"Authorization": f"Bearer {self.access_token}"}
    
    def get_auth_token(self):
        try:
            response = requests.request(
                url=f"{self.settings.fedex_server_url}{self.settings.fedex_oauth_token_url}",
                method="POST",
                data = {
                    "grant_type": "client_credentials",
                    "client_id" : self.settings.fedex_client_id,
                    "client_secret" : self.settings.fedex_client_secret
                },
                headers = {
                    "Content-Type": "application/x-www-form-urlencoded",
                }
            )
            data = frappe._dict(response.json())
            expire_time = cint(data.expires_in)
            frappe.cache().set_value(
                self.ACCESS_TOKEN_KEY,
                data.access_token,
                expires_in_sec=expire_time-300,
            )
            return data.access_token
        except Exception as e:
            frappe.log_error(
                title="FedEx OAuth Token Generation Failed",
                message=frappe.get_traceback(),
            )

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
                try:
                    error = response.content.decode("utf-8", errors="replace")
                except Exception:
                    error = str(response.content)
            else:
                error = f"status_code: {response.status_code}",
            frappe.log_error(
                title=f"FedEx API Failed",
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

@frappe.whitelist()
def fetch_fedex_tracking_details(dn):
    api_type = None
    accountNumber = None

    client = FedExIntegration()
    headers = client.headers
    headers.update({
        'content-type': 'application/json',
        'x-locale': client.settings.x_locale or 'en_US'
    })

    doc = frappe.get_doc("Delivery Note", dn)
    source_warehouse = doc.set_warehouse
    for acc in client.settings.account_number_details:
        if acc.source_warehouse == source_warehouse:
            accountNumber = acc.account_number
            break

    if doc.tracking_number == None:
        api_type = "By Reference"
        ENDPOINT_URL = f"{client.settings.fedex_server_url}{client.settings.fedex_track_by_reference_number_url}"
        headers.update({
            'x-customer-transaction-id': str(uuid.uuid4())
        })
        payload = {
            "referencesInformation": {
                "type": client.settings.reference_type or "SHIPPER_REFERENCE",
                "value": dn,
                "accountNumber": accountNumber,
                "carrierCode": "FXFR",
                "shipDateBegin": getdate(add_to_date(doc.posting_date, days=-2)).strftime("%Y-%m-%d"),
                "shipDateEnd": getdate(add_to_date(doc.posting_date, days=int(client.settings.check_for_no_of_days))).strftime("%Y-%m-%d"),
            },
            "includeDetailedScans": client.settings.include_detailed_scans_in_response or "True"
        }
       
    elif doc.tracking_number != None:
        api_type = "By Tracking ID"
        tracking_id = doc.tracking_number
        ENDPOINT_URL = f"{client.settings.fedex_server_url}{client.settings.fedex_track_by_tracking_id}"
        payload = {
                "includeDetailedScans": client.settings.include_detailed_scan or "True",
                "trackingInfo": [
                    {
                        "trackingNumberInfo": {
                            "trackingNumber": tracking_id,
                    }
                }
            ]
        }

    response, error = make_api_request(
        method="POST", 
        url=ENDPOINT_URL, 
        headers=headers, 
        json_data=payload, 
        params=None, 
        service_name="FedEx Tracking API", 
        log_args={ "url" : ENDPOINT_URL, "type" : api_type}
    )

    update_delivery_note_with_fedex_details(dn=dn, response=response, error=error, api_type=api_type)

def create_map_with_description():
    """
    Returns Code and Corresponding Jammy Status Mapping For Error and Success Codes
    """
    error_map = {}
    success_map = {}
    setting_doc = frappe.get_doc("Parcel Service Settings")

    for err in setting_doc.error_code_description:
        error_map.update({ err.fedex_error_code : err.jammy_description })

    for sc in setting_doc.tracking_code_description:
        success_map.update({ sc.fedex_status_code : sc.jammy_description })

    return success_map, error_map

def update_delivery_note_with_fedex_details(dn, response, error, api_type):
    document = frappe.get_doc("Delivery Note", dn)
    success_map, error_map = create_map_with_description()
    
    if error:
        """
        Create Error Log and Stop API Call
        """
        error = json.loads(error)
        log = frappe.log_error(
            title = "FedEx Tracking API Failed",
            message = f"Delivery Note: {dn} \nError Details: \n {json.dumps(error, indent=4)}\n"
        )
        document.custom_tracking = "Exception"
        document.save(ignore_permissions = True)
        frappe.msgprint("Error While Collecting Data From API! For More Details: {0}".format(get_link_to_form("Error Log", log.name)), indicator = "red")
        return
    
    if response:
        if response.get('output') and response['output'].get('completeTrackResults'):
            track_results = response.get('output').get('completeTrackResults')[0]
            if track_results and 'latestStatusDetail' not in track_results.get('trackResults')[0].keys():
                document.custom_tracking_code = track_results.get('trackResults')[0].get('error').get('code')
                document.custom_tracking = error_map.get(track_results.get('trackResults')[0].get('error').get('code'))
                document.save(ignore_permissions = True)
                frappe.msgprint("Exception Occurs While Fetching Data", indicator = "red", alert = True)
                return
            
            if track_results and api_type == "By Reference":
                document.custom_last_api_call = now()
                document.tracking_number = track_results.get('trackingNumber')
                document.custom_tracking_code = track_results.get('trackResults')[0].get('latestStatusDetail').get('code')
                document.custom_tracking = success_map.get(track_results.get('trackResults')[0].get('statusCode')) or track_results.get('trackResults')[0].get('latestStatusDetail').get('description')
                if document.custom_tracking == "Processing" and document.custom_last_date_for_processing_status == None:
                    document.custom_last_date_for_processing_status = today()
                elif document.custom_tracking != "Processing":
                    document.custom_last_date_for_processing_status = None

                document.save(ignore_permissions = True)
                frappe.msgprint("Tracking Details For Reference {0} Updated Successfully!".format(dn), indicator = "green", alert = True)

            elif track_results and api_type == "By Tracking ID":
                document.custom_last_api_call = now()
                document.custom_tracking_code = track_results.get('trackResults')[0].get('latestStatusDetail').get('code')
                document.custom_tracking = success_map.get(track_results.get('trackResults')[0].get('statusCode')) or track_results.get('trackResults')[0].get('latestStatusDetail').get('description')
                if document.custom_tracking == "Processing" and document.custom_last_date_for_processing_status == None:
                    document.custom_last_date_for_processing_status = today()
                elif document.custom_tracking != "Processing":
                    document.custom_last_date_for_processing_status = None

                document.save(ignore_permissions = True)
                frappe.msgprint("Tracking Details For Tracing ID {0} Updated Successfully!".format(document.tracking_number), indicator = "green", alert = True)

def check_and_update_eligible_delivery_note_by_scheduler():
    print("Running Scheduler")
    settings_doc = frappe.get_doc("Parcel Service Settings")
    start_date = today()
    end_date = add_to_date(start_date, days= -int(settings_doc.check_past_no_of_days_for_scheduler))

    eligible_delivery_notes = frappe.db.sql('''
        SELECT dn.name 
        FROM `tabDelivery Note` dn
        WHERE dn.posting_date BETWEEN '{0}' AND '{1}'
        AND dn.ship_via LIKE "%FED%"
        AND dn.docstatus = 1
        AND (dn.custom_tracking = "Processing" OR dn.custom_tracking = "In Transit" OR dn.custom_tracking = "In Transit, Delayed" OR dn.custom_tracking = "Split In Transit" OR dn.custom_tracking = "Exception" OR dn.custom_tracking IS NULL);'''
        .format(end_date, start_date),
        as_dict = 1
    )
    
    if len(eligible_delivery_notes) > 0:
        for dn in eligible_delivery_notes:
            delivery_note = dn['name']
            fetch_fedex_tracking_details(delivery_note)

def fill_status_code_details_in_parcel_service_settings():
    settings_doc = frappe.get_doc("Parcel Service Settings")
    success_codes = [
        {
            "fedex_status_code": "AA",
            "fedex_code_description": "At Airport",
            "jammy_description": "In Transit"
        },
        {
            "fedex_status_code": "AC",
            "fedex_code_description": "At Canada Post facility",
            "jammy_description": "In Transit"
        },
        {
            "fedex_status_code": "AD",
            "fedex_code_description": "At Delivery",
            "jammy_description": "In Transit"
        },
        {
            "fedex_status_code": "AF",
            "fedex_code_description": "At local FedEx Facility",
            "jammy_description": "In Transit"
        },
        {
            "fedex_status_code": "AO",
            "fedex_code_description": "Shipment arriving On-time",
            "jammy_description": "In Transit"
        },
        {
            "fedex_status_code": "AP",
            "fedex_code_description": "At Pickup",
            "jammy_description": "In Transit"
        },
        {
            "fedex_status_code": "AR",
            "fedex_code_description": "Arrived at FedEx location",
            "jammy_description": "In Transit"
        },
        {
            "fedex_status_code": "AX",
            "fedex_code_description": "At USPS facility",
            "jammy_description": "In Transit"
        },
        {
            "fedex_status_code": "CA",
            "fedex_code_description": "Shipment Cancelled",
            "jammy_description": "Cancelled"
        },
        {
            "fedex_status_code": "CH",
            "fedex_code_description": "Location Changed",
            "jammy_description": "In Transit"
        },
        {
            "fedex_status_code": "DD",
            "fedex_code_description": "Delivery Delay",
            "jammy_description": "In Transit, Delayed"
        },
        {
            "fedex_status_code": "DE",
            "fedex_code_description": "Delivery Exception",
            "jammy_description": "Exception"
        },
        {
            "fedex_status_code": "DL",
            "fedex_code_description": "Delivered",
            "jammy_description": "Delivered"
        },
        {
            "fedex_status_code": "DP",
            "fedex_code_description": "Departed",
            "jammy_description": "In Transit"
        },
        {
            "fedex_status_code": "DR",
            "fedex_code_description": "Vehicle furnished but not used",
            "jammy_description": "In Transit"
        },
        {
            "fedex_status_code": "DS",
            "fedex_code_description": "Vehicle Dispatched",
            "jammy_description": "In Transit"
        },
        {
            "fedex_status_code": "DY",
            "fedex_code_description": "Delay",
            "jammy_description": "In Transit, Delayed"
        },
        {
            "fedex_status_code": "EA",
            "fedex_code_description": "Enroute to Airport",
            "jammy_description": "In Transit"
        },
        {
            "fedex_status_code": "ED",
            "fedex_code_description": "Enroute to Delivery",
            "jammy_description": "In Transit"
        },
        {
            "fedex_status_code": "EO",
            "fedex_code_description": "Enroute to Origin Airport",
            "jammy_description": "In Transit"
        },
        {
            "fedex_status_code": "EP",
            "fedex_code_description": "Enroute to Pickup",
            "jammy_description": "In Transit"
        },
        {
            "fedex_status_code": "FD",
            "fedex_code_description": "At FedEx Destination",
            "jammy_description": "In Transit"
        },
        {
            "fedex_status_code": "HL",
            "fedex_code_description": "Hold at Location",
            "jammy_description": "In Transit"
        },
        {
            "fedex_status_code": "HP",
            "fedex_code_description": "Ready for Recipient Pickup",
            "jammy_description": "In Transit"
        },
        {
            "fedex_status_code": "IT",
            "fedex_code_description": "In Transit",
            "jammy_description": "In Transit"
        },
        {
            "fedex_status_code": "IX",
            "fedex_code_description": "In transit (see Details)",
            "jammy_description": "In Transit"
        },
        {
            "fedex_status_code": "LO",
            "fedex_code_description": "Left Origin",
            "jammy_description": "In Transit"
        },
        {
            "fedex_status_code": "OC",
            "fedex_code_description": "Order Created",
            "jammy_description": "Processing"
        },
        {
            "fedex_status_code": "OD",
            "fedex_code_description": "Out for Delivery",
            "jammy_description": "In Transit"
        },
        {
            "fedex_status_code": "OF",
            "fedex_code_description": "At FedEx origin facility",
            "jammy_description": "In Transit"
        },
        {
            "fedex_status_code": "OX",
            "fedex_code_description": "Shipment information sent to USPS",
            "jammy_description": "Processing"
        },
        {
            "fedex_status_code": "PD",
            "fedex_code_description": "Pickup Delay",
            "jammy_description": "Processing"
        },
        {
            "fedex_status_code": "PF",
            "fedex_code_description": "Plane in Flight",
            "jammy_description": "In Transit"
        },
        {
            "fedex_status_code": "PL",
            "fedex_code_description": "Plane Landed",
            "jammy_description": "In Transit"
        },
        {
            "fedex_status_code": "PM",
            "fedex_code_description": "In Progress",
            "jammy_description": "In Transit"
        },
        {
            "fedex_status_code": "PU",
            "fedex_code_description": "Picked Up",
            "jammy_description": "In Transit"
        },
        {
            "fedex_status_code": "PX",
            "fedex_code_description": "Picked up (see Details)",
            "jammy_description": "In Transit"
        },
        {
            "fedex_status_code": "RR",
            "fedex_code_description": "CDO requested",
            "jammy_description": "In Transit"
        },
        {
            "fedex_status_code": "RM",
            "fedex_code_description": "CDO Modified",
            "jammy_description": "In Transit"
        },
        {
            "fedex_status_code": "RC",
            "fedex_code_description": "CDO Cancelled",
            "jammy_description": "In Transit"
        },
        {
            "fedex_status_code": "RS",
            "fedex_code_description": "Return to Shipper",
            "jammy_description": "Exception"
        },
        {
            "fedex_status_code": "RP",
            "fedex_code_description": "Return label link emailed to return sender",
            "jammy_description": "Processing"
        },
        {
            "fedex_status_code": "LP",
            "fedex_code_description": "Return label link cancelled by shipment originator",
            "jammy_description": "Cancelled"
        },
        {
            "fedex_status_code": "RG",
            "fedex_code_description": "Return label link expiring soon",
            "jammy_description": "Processing"
        },
        {
            "fedex_status_code": "RD",
            "fedex_code_description": "Return label link expired",
            "jammy_description": "Cancelled"
        },
        {
            "fedex_status_code": "SE",
            "fedex_code_description": "Shipment Exception",
            "jammy_description": "Exception"
        },
        {
            "fedex_status_code": "SF",
            "fedex_code_description": "At Sort Facility",
            "jammy_description": "In Transit"
        },
        {
            "fedex_status_code": "SP",
            "fedex_code_description": "Split Status",
            "jammy_description": "Split In Transit"
        },
        {
            "fedex_status_code": "TR",
            "fedex_code_description": "Transfer",
            "jammy_description": "In Transit"
        },
        {
            "fedex_status_code": "CC",
            "fedex_code_description": "Cleared Customs",
            "jammy_description": "In Transit"
        },
        {
            "fedex_status_code": "CD",
            "fedex_code_description": "Clearance Delay",
            "jammy_description": "In Transit, Delayed"
        },
        {
            "fedex_status_code": "CP",
            "fedex_code_description": "Clearance in Progress",
            "jammy_description": "In Transit"
        },
        {
            "fedex_status_code": "EA",
            "fedex_code_description": "Export Approved",
            "jammy_description": "In Transit"
        },
        {
            "fedex_status_code": "SP",
            "fedex_code_description": "Split Status",
            "jammy_description": "Split In Tranist"
        },
        {
            "fedex_status_code": "CA",
            "fedex_code_description": "Carrier",
            "jammy_description": "Carrier"
        },
        {
            "fedex_status_code": "RC",
            "fedex_code_description": "Recipient",
            "jammy_description": "Recipient"
        },
        {
            "fedex_status_code": "SH",
            "fedex_code_description": "Shipper",
            "jammy_description": "Shipper"
        },
        {
            "fedex_status_code": "CU",
            "fedex_code_description": "Customs",
            "jammy_description": "Customs"
        },
        {
            "fedex_status_code": "BR",
            "fedex_code_description": "Broker",
            "jammy_description": "Broker"
        },
        {
            "fedex_status_code": "TP",
            "fedex_code_description": "Transfer Partner",
            "jammy_description": "Transfer Partner"
        },
        {
            "fedex_status_code": "SP",
            "fedex_code_description": "Split status",
            "jammy_description": "Split In Transit"
        }
    ]
    
    error_codes = [
        {
            "fedex_error_code": "CUSTOMER.REVOKE.REQUIRED",
            "fedex_code_description": "Customer has been revoked to view invited shipments.",
            "jammy_description": "Exception"
        },
        {
            "fedex_error_code": "CUSTOMER.SIZE.INVALID",
            "fedex_code_description": "Extraordinary sized customer.",
            "jammy_description": "Exception"
        },
        {
            "fedex_error_code": "CUSTOMER.USAGE.LOCKED",
            "fedex_code_description": "Customer is locked out.",
            "jammy_description": "Exception"
        },
        {
            "fedex_error_code": "REFERENCETRACKING.SHIPDATERANGE.INVALID",
            "fedex_code_description": "Please provide a valid ship date range as a part of search criteria when entering account number.",
            "jammy_description": "Exception"
        },
        {
            "fedex_error_code": "TRACKING.ACCOUNTNUMBER.EMPTY",
            "fedex_code_description": "If not providing FedEx account number, please enter destination country/territory and postal code.",
            "jammy_description": "Exception"
        },
        {
            "fedex_error_code": "TRACKING.CUSTOMCRITICAL.ERROR",
            "fedex_code_description": "For tracking information, please log in to customcritical.fedex.com or contact Customer Service at 1.866.274.6117.",
            "jammy_description": "Service Error, See fedex.com"
        },
        {
            "fedex_error_code": "TRACKING.DATA.NOTUNIQUE",
            "fedex_code_description": "A unique match was not found. Please resubmit your request with a FedEx service or enter your FedEx account number.",
            "jammy_description": "Exception"
        },
        {
            "fedex_error_code": "TRACKING.DESTINATIONCOUNTRYCODE.INVALID",
            "fedex_code_description": "Please provide a valid destination country/territory code.",
            "jammy_description": "Exception"
        },
        {
            "fedex_error_code": "TRACKING.MULTISTOP.ERROR",
            "fedex_code_description": "For tracking information, please log in to customcritical.fedex.com or contact Customer Service at 1.866.274.6117.",
            "jammy_description": "Service Error, See fedex.com"
        },
        {
            "fedex_error_code": "TRACKING.POSTALCODE.INVALID",
            "fedex_code_description": "Please provide a valid postal code.",
            "jammy_description": "Exception"
        },
        {
            "fedex_error_code": "TRACKING.REFERENCEDATA.INCOMPLETE",
            "fedex_code_description": "Please enter an account number or destination country/territory and postal code.",
            "jammy_description": "Exception"
        },
        {
            "fedex_error_code": "TRACKING.REFERENCENUMBER.NOTFOUND",
            "fedex_code_description": "Reference number cannot be found. Please correct the reference number and try again.",
            "jammy_description": "Exception"
        },
        {
            "fedex_error_code": "TRACKING.REFERENCETYPE.INVALID",
            "fedex_code_description": "Please provide a valid reference/associated type.",
            "jammy_description": "Exception"
        },
        {
            "fedex_error_code": "TRACKING.REFERENCEVALUE.EMPTY",
            "fedex_code_description": "Missing or invalid shipment. Please enter a valid shipment number.",
            "jammy_description": "Exception"
        },
        {
            "fedex_error_code": "TRACKING.REFRENCEVALUE.INVALID",
            "fedex_code_description": "Invalid reference number. Please correct the request and try again.",
            "jammy_description": "Exception"
        },
        {
            "fedex_error_code": "TRACKING.SHIPDATE.ENDDATEBEFOREBEGINDATE",
            "fedex_code_description": "Invalid ship date range. End date should not be before begin date.",
            "jammy_description": "Exception"
        },
        {
            "fedex_error_code": "TRACKING.SHIPDATEBEGIN.INVALID",
            "fedex_code_description": "Please provide valid ship begin date.",
            "jammy_description": "Exception"
        },
        {
            "fedex_error_code": "TRACKING.SHIPDATEBEGIN.TOOOLD",
            "fedex_code_description": "We are unable to provide tracking information. Begin date is too far in the past.",
            "jammy_description": "Exception"
        },
        {
            "fedex_error_code": "TRACKING.SHIPDATEEND.FUTURE",
            "fedex_code_description": "Invalid ship date range. End date must not be in the future.",
            "jammy_description": "Exception"
        },
        {
            "fedex_error_code": "TRACKING.SHIPDATEEND.INVALID",
            "fedex_code_description": "Please provide valid ship end date.",
            "jammy_description": "Exception"
        },
        {
            "fedex_error_code": "TRACKING.SHIPDATERANGE.ERROR",
            "fedex_code_description": "Invalid date range. Please check for following conditions: 1. End date is before Begin date. 2. Begin date is beyond 2 years. 3. Begin to End date exceeds 30 days.",
            "jammy_description": "Exception"
        },
        {
            "fedex_error_code": "TRACKING.SHIPDATERANGE.INVALID",
            "fedex_code_description": "Invalid ship date range. Please provide valid ship begin and end date.",
            "jammy_description": "Exception"
        },
        {
            "fedex_error_code": "TRACKING.SHIPDATERANGE.TOOLONG",
            "fedex_code_description": "Ship date range is too long. Please reduce the range and try again.",
            "jammy_description": "Exception"
        },
        {
            "fedex_error_code": "TRACKING.TCN.NOTFOUND",
            "fedex_code_description": "Transportation control number cannot be found. Please correct the transportation control number and try again.",
            "jammy_description": "Exception"
        },
        {
            "fedex_error_code": "TRACKING.TCNVALUE.EMPTY",
            "fedex_code_description": "Please provide a valid Transportation Control Number.",
            "jammy_description": "Exception"
        },
        {
            "fedex_error_code": "TRACKING.TRACKINGNUMBER.EMPTY",
            "fedex_code_description": "Please provide tracking number.",
            "jammy_description": "Exception"
        },
        {
            "fedex_error_code": "TRACKING.TRACKINGNUMBER.INVALID",
            "fedex_code_description": "Invalid tracking number. Please correct the tracking number format and try again.",
            "jammy_description": "Exception"
        },
        {
            "fedex_error_code": "TRACKING.TRACKINGNUMBER.NOTFOUND",
            "fedex_code_description": "Tracking number cannot be found. Please correct the tracking number and try again.",
            "jammy_description": "Exception"
        },
        {
            "fedex_error_code": "TRACKING.TRACKINGNUMBERS.LIMITEXCEEDED",
            "fedex_code_description": "Please limit your inquiry to 30 tracking numbers or references.",
            "jammy_description": "Exception"
        },
        {
            "fedex_error_code": "USER.RELOGIN.REQUIRED",
            "fedex_code_description": "We are unable to process this shipment for the moment. Try again later or contact FedEx Customer Service.",
            "jammy_description": "Service Error, See fedex.com"
        },
        {
            "fedex_error_code": "INTERNAL.SERVER.ERROR",
            "fedex_code_description": "We encountered an unexpected error and are working to resolve the issue. We apologize for any inconvenience. Please check back at a later time.",
            "jammy_description": "Service Error, See fedex.com"
        },
        {
            "fedex_error_code": "TRACKING.MULTIPIECE.ERROR",
            "fedex_code_description": "We are unable to provide notifications because either the package is too old or there is more than one package with the provided tracking number.",
            "jammy_description": "Exception"
        },
        {
            "fedex_error_code": "NOTIFICATION.TRACKINGNBR.NOTFOUND",
            "fedex_code_description": "Tracking number cannot be found. Please update and try again.",
            "jammy_description": "Exception"
        },
        {
            "fedex_error_code": "TRACKING.EMAILADDRESS.INVALID",
            "fedex_code_description": "One or more of the Email addresses you entered is invalid. Please update and try again.",
            "jammy_description": "Exception"
        },
        {
            "fedex_error_code": "TRACKING.LOCALE.INVALID",
            "fedex_code_description": "Requested localization is invalid or not supported. Please update and try again.",
            "jammy_description": "Exception"
        },
        {
            "fedex_error_code": "TRACKING.SENDERCONTACTNAME.INVALID",
            "fedex_code_description": "Sender contact name is missing or invalid. Please update and try again.",
            "jammy_description": "Exception"
        },
        {
            "fedex_error_code": "TRACKING.SENDEREMAILADDRESS.INVALID",
            "fedex_code_description": "Sender email address is missing or invalid. Please update and try again.",
            "jammy_description": "Exception"
        },
        {
            "fedex_error_code": "TRACKINGDOCUMENT.DOCUMENT.UNAVAILABLE",
            "fedex_code_description": "Signature Proof of Delivery is not currently available for this Tracking Number. Availability of signature images may take up to 5 days after delivery date. Please try later.",
            "jammy_description": "Exception"
        }
    ]
    if settings_doc.tracking_code_description == []:
        print("Filling FedEx Tracking Status Code Details in Parcel Service Settings..")
        for sc in success_codes:
            settings_doc.append('tracking_code_description', {
                'fedex_status_code': sc['fedex_status_code'],
                'fedex_code_description': sc['fedex_code_description'],
                'jammy_description': sc['jammy_description']
            })

    if settings_doc.error_code_description == []:
        print("Filling FedEx Error Code Details in Parcel Service Settings..")
        for ec in error_codes:
            settings_doc.append('error_code_description', {
                'fedex_error_code' : ec['fedex_error_code'],
                'fedex_code_description' : ec['fedex_code_description'],
                'jammy_description': ec['jammy_description']
            })
    settings_doc.save(ignore_permissions=True)
