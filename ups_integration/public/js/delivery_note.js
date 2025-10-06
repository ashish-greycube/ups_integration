frappe.ui.form.on("Delivery Note", {
    refresh: function(frm) {
        if (frm.doc.docstatus == 1 && frm.doc.ship_via.startsWith("UPS")) {
            frm.add_custom_button("Get UPS Details", function() {
                frappe.call({
                    method: 'ups_integration.api.get_ups_tracking_data',
                    args: {
                        'delivery_note' : frm.doc.name
                    },
                    callback: function(res) {
                        let response = res.message
                        console.log(response)
                    }
                })
            })
        }
    }
});