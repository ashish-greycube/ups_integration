import json
import frappe
import requests
from frappe.integrations.utils import create_request_log
from frappe.utils import get_datetime, today, add_to_date, get_link_to_form, now

class PriorityIntegration:
    """ 
    get access token and cache for expiry time. 
    """
    def __init__(self) -> None:
        self.ACCESS_TOKEN_KEY = 'priority_access_token'
        self.settings = frappe.get_doc("Parcel Service Settings")
        self.__initialize_auth()

    def __initialize_auth(self):
        """
        Initialize and setup authentication details
        """
        self.access_token = frappe.cache().get_value(self.ACCESS_TOKEN_KEY)
        if not self.access_token:
            self.access_token = self.get_auth_token()
        self.headers = {"X-API-KEY": f"{self.access_token}"}
    
    def get_auth_token(self):
        try:
            access_token = self.settings.priority_api_key
            frappe.cache().set_value(
                self.ACCESS_TOKEN_KEY,
                access_token,
            )
            return access_token
        except Exception as e:
            frappe.log_error(
                title="Priority OAuth Token Generation Failed",
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
                    error = {
                        'content' : response.content.decode("utf-8", errors="replace"),
                        'code' : response.status_code
                    }
                except Exception:
                    error = {
                        'content' : str(response.content),
                        'code' : response.status_code
                    }
            else:
                error = {
                        'code' : response.status_code
                    }
            frappe.log_error(
                title=f"Priority API Failed",
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
def fetch_priority_tracking_details(dn):
    """
    It will use provided DN Number to track data from Priority System.  
    """
    client = PriorityIntegration()
    headers = client.headers
    headers.update({
        "Content-Type": "application/json"
    })

    ENDPOINT_URL = f"{client.settings.priority_base_url}{client.settings.priority_tracking_url}"

    json={
      "identifierType": client.settings.shipment_identifier_type,
      "identifierValue": dn
    }

    response, error = make_api_request(
        method="POST", 
        url=ENDPOINT_URL, 
        headers=headers, 
        json_data=json, 
        params=None, 
        service_name="Priority Tracking API", 
        log_args={ "url" : ENDPOINT_URL}
    )

    update_delivery_note_with_priority_details(dn=dn, response=response, error=error)

def create_map_with_description():
    """
    Returns Code and Corresponding Jammy Status Mapping For Success & Error Codes
    """
    success_map = {}
    error_map = {}
    setting_doc = frappe.get_doc("Parcel Service Settings")

    for rc in setting_doc.response_code_details:
        success_map.update({ rc.priority_status_description : rc.jammy_description })

    for em in setting_doc.error_code_details:
        error_map.update({ em.response_code : {'priority_error_description' : em.priority_error_code_description, 'jammy_description' : em.jammy_description }})

    return success_map, error_map

def update_delivery_note_with_priority_details(dn, response, error):
    document = frappe.get_doc("Delivery Note", dn)
    success_map, error_map = create_map_with_description()

    if error:
        """
        Create Error Log and Stop API Call.
        """
        if isinstance(error, dict):
            if error.get('code') == 401:
                desc = error_map.get(f"{error.get('code')}")
                log = frappe.log_error(
                    title = "Priority Tracking API Failed",
                    message = f"Delivery Note: {dn} \nError Details: \n {json.dumps(desc, indent=4)}\n"
                )
                document.custom_tracking = desc.get("jammy_description")
                document.save(ignore_permissions = True)
                frappe.msgprint("Error While Collecting Data From API! For More Details: {0}".format(get_link_to_form("Error Log", log.name)), indicator = "red")
                return
            else:
                desc = error_map.get(f"{error.get('code')}")
                log = frappe.log_error(
                    title = "Priority Tracking API Failed",
                    message = f"Delivery Note: {dn} \nError Details: \n {json.dumps(error, indent=2)}\n"
                )
                document.custom_tracking = desc.get("jammy_description")
                if error.get('code') == 500:
                    if document.custom_incident_first_date == None:
                        document.custom_incident_first_date = today()

                document.save(ignore_permissions = True)
                frappe.msgprint("Error While Collecting Data From API! For More Details: {0}".format(get_link_to_form("Error Log", log.name)), indicator = "red")
                return
        else:
            log = frappe.log_error(
                title = "Priority Tracking API Failed",
                message = f"Delivery Note: {dn} \nError Details: \n {error}\n"
            )
            document.custom_tracking = 'Exception'
            document.save(ignore_permissions = True)
            frappe.msgprint("Error While Collecting Data From API! For More Details: {0}".format(get_link_to_form("Error Log", log.name)), indicator = "red")
            return

    if response:
        if response.get("shipments"):
            track_results = response.get("shipments")[0]
            if track_results:
                document.tracking_number = track_results.get('id')
                document.custom_tracking_code = '200'
                document.custom_tracking = success_map.get(track_results.get('status'))
                document.custom_last_api_call = now()
                document.save(ignore_permissions = True)
                frappe.msgprint("Latest Shipment Details Of Ref {0} Updated Successfully!".format(document.name), indicator = "green", alert = True)
                return

def check_and_update_eligible_delivery_note_by_scheduler():
    print("Scheduler Started For Updating Delivery Note...")
    settings_doc = frappe.get_doc("Parcel Service Settings")
    start_date = today()
    end_date = add_to_date(start_date, days= -int(settings_doc.past_no_of_days_for_scheduler))

    eligible_delivery_notes = frappe.db.sql('''
        SELECT dn.name 
        FROM `tabDelivery Note` dn
        WHERE dn.posting_date BETWEEN '{0}' AND '{1}'
        AND dn.ship_via LIKE "PRIORITY%"
        AND dn.docstatus = 1
        AND (dn.custom_tracking = "Processing" OR dn.custom_tracking = "In Transit" OR dn.custom_tracking = "Exception" OR dn.custom_tracking IS NULL);'''
        .format(end_date, start_date),
        as_dict = 1
    )

    exceptional_delivery_notes = frappe.db.sql('''
        SELECT dn.name 
        FROM `tabDelivery Note` dn
        WHERE dn.posting_date BETWEEN '{0}' AND '{1}'
        AND dn.ship_via LIKE "PRIORITY%"
        AND dn.docstatus = 1
        AND dn.custom_tracking = "DN Not Found" AND DATE_ADD(dn.custom_incident_first_date, INTERVAL 6 DAY) > CURDATE();'''
        .format(end_date, start_date),
        as_dict = 1
    )

    if len(eligible_delivery_notes) > 0:
        print("Updating Eligible Delivery Notes Via Scheduler")
        for dn in eligible_delivery_notes:
            delivery_note = dn['name']
            fetch_priority_tracking_details(delivery_note)

    if len(exceptional_delivery_notes) > 0:
        print("Updating Exceptional Eligible Delivery Notes Via Scheduler")
        for dn in exceptional_delivery_notes:
            delivery_note = dn['name']
            fetch_priority_tracking_details(delivery_note)

def fillup_api_responce_code_details():
    settings = frappe.get_doc("Parcel Service Settings")
    success_codes = [
        {
            "response_code": "200",
            "priority_status_description": "Dispatched",
            "jammy_description": "Processing"
        },
        {
            "response_code": "200",
            "priority_status_description": "In Transit",
            "jammy_description": "In Transit"
        },
        {
            "response_code": "200",
            "priority_status_description": "Delivered",
            "jammy_description": "Delivered"
        },
        {
            "response_code": "200",
            "priority_status_description": "Canceled",
            "jammy_description": "Canceled"
        },
        {
            "response_code": "200",
            "priority_status_description": "Exception",
            "jammy_description": "Exception"
        }
    ]
    error_codes = [
        {
            "response_code": "400",
            "priority_error_code_description": "Error converting value \\\"SALES_aORDER\\\" to type 'Priority1.API.Areas.LTL.Models.V2.IdentifierType'. Path 'identifierType', line 2, position 34.",
            "jammy_description": "Exception"
        },
        {
            "response_code": "401",
            "priority_error_code_description": "Authorization Error: Incorrect API Key Send.",
            "jammy_description": "Exception"
        },
        {
            "response_code": "500",
            "priority_error_code_description": "No shipments found matching identifier type PurchaseOrder with value DN-5361s1",
            "jammy_description": "DN Not Found"
        }
    ]

    if settings.response_code_details == []:
        print("Filling Priority Tracking Status Code Details in Parcel Service Settings..")
        for sc in success_codes:
            settings.append('response_code_details', {
                'response_code': sc['response_code'],
                'priority_status_description': sc['priority_status_description'],
                'jammy_description': sc['jammy_description']
            })

    if settings.error_code_details == []:
        print("Filling Priority Error Code Details in Parcel Service Settings..")
        for ec in error_codes:
            settings.append('error_code_details', {
                'response_code' : ec['response_code'],
                'priority_error_code_description' : ec['priority_error_code_description'],
                'jammy_description': ec['jammy_description']
            })
    settings.save(ignore_permissions=True)