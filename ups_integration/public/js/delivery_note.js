frappe.ui.form.on("Delivery Note", {
    refresh: function (frm) {
        if (frm.doc.docstatus == 1 && frm.doc.ship_via.startsWith("UPS")) {
            frm.add_custom_button("Get UPS Details", function () {
                frappe.call({
                    method: 'ups_integration.api.get_ups_tracking_data',
                    args: {
                        'delivery_note': frm.doc.name
                    },
                    callback: function (res) {
                        let response = res.message
                        console.log(response)
                    }
                })
            })
        }

        if (frm.doc.docstatus == 1 && frm.doc.ship_via.toLowerCase().startsWith("fed")) {
            frm.add_custom_button("Get FedEx Details", function () {
                frappe.call({
                    method: 'ups_integration.fedex_integration.fetch_fedex_tracking_details',
                    args: {
                        'dn': frm.doc.name
                    },
                    callback: function (res) {
                        let response = res.message
                        console.log(response)
                    }
                })
            })
        }

        if (frm.doc.docstatus == 1 && frm.doc.ship_via.startsWith("PRIORITY ")) {
            frm.add_custom_button("Get Priority Details", function () {
                frappe.call({
                    method: 'ups_integration.priority_integration.fetch_priority_tracking_details',
                    args: {
                        'dn': frm.doc.name
                    },
                    callback: function (res) {
                        console.log(res);
                    }
                })
            });
        }
    }
});