import frappe
from frappe import _
from hrms.payroll.doctype.payroll_entry.payroll_entry import PayrollEntry
from erpnext.accounts.doctype.accounting_dimension.accounting_dimension import (
    get_accounting_dimensions,
)
from frappe.utils import flt
from erpnext import get_company_currency


class PayrollEntryOverride(PayrollEntry):

    def get_salary_components_with_project(self, component_type):
        salary_slips = self.get_sal_slip_list(ss_status=1, as_dict=True)
        if salary_slips:
            salary_slips_names = [d.name for d in salary_slips]
            placeholders = ", ".join(["%s"] * len(salary_slips_names))
            salary_components = frappe.db.sql(
                f"""
                select ssd.salary_component, ssd.amount, ssd.parentfield, ss.employee, ss.start_date, ss.end_date
                from `tabSalary Slip` ss, `tabSalary Detail` ssd
                where ss.name = ssd.parent and ssd.parentfield = %s and ss.name in ({placeholders})
                """,
                [component_type] + salary_slips_names,
                as_dict=True,
            )
            return self.set_employee_ammount_with_project_account_dimention(
                salary_components
            )

    def get_account(self, component_dict = None):
        if not self.is_project_payroll_:
            return super().get_account()
        account_dict = {}
        for key, amount in component_dict.items():
                account = self.get_salary_component_account(key[0])
                account_dict[(account, key[1], key[2])] = account_dict.get((account, key[1], key[2]), 0) + amount
        return account_dict

    def get_salary_component_total_with_project(self, component_type):
        salary_components = self.get_salary_components_with_project(component_type)
        if salary_components:
            component_dict = {}
            for item in salary_components:
                add_component_to_accrual_jv_entry = True
                if component_type == "earnings":
                    is_flexible_benefit, only_tax_impact = frappe.db.get_value(
                        "Salary Component",
                        item["salary_component"],
                        ["is_flexible_benefit", "only_tax_impact"],
                    )
                    if is_flexible_benefit == 1 and only_tax_impact == 1:
                        add_component_to_accrual_jv_entry = False
                if add_component_to_accrual_jv_entry:
                    cost_center = item.payroll_cost_center
                    if item.cost_center:
                        cost_center = item.cost_center

                    if item.project:
                        # Key: (Salary Component, Project, Cost Center)
                        key = (item.salary_component, item.project, cost_center)
                    else:
                        # Key: (Salary Component, None, Cost Center)
                        key = (item.salary_component, None, cost_center)

                    component_dict[key] = component_dict.get(key, 0) + flt(item.amount)

            # Result account_details keys: (Account, Project, Cost Center)
            account_details = self.get_account(component_dict=component_dict)
            return account_details
        return {}


    def set_employee_ammount_with_project_account_dimention(self, salary_slips):
        salary_slips_with_project = []
        for i in salary_slips:
            projects = None
            employee_project = frappe.get_all(
                "Employee Projects Payroll",
                filters={
                    "docstatus": 1,
                    "employee": i["employee"],
                    "from_date": ["<=", i["start_date"]],
                    "to_date": [">=", i["end_date"]],
                },
                fields=["name"],
            )
            if employee_project:
                projects = frappe.get_all(
                    "Employee Project",
                    filters={"parent": employee_project[0]["name"]},
                    fields=["project", "cost_center", "percent_pay"],
                )
            
            # Get employee's default payroll cost center
            employee_doc = frappe.get_cached_doc('Employee', i["employee"])
            payroll_cost_center = employee_doc.payroll_cost_center or self.cost_center

            if projects:
                amount = i["amount"]
                for p in projects:
                    sal_slip = i.copy()
                    sal_slip["amount"] = amount * (p["percent_pay"] / 100)
                    sal_slip["project"] = p["project"]
                    # If project cost center is provided, use it, otherwise use employee's default
                    sal_slip["cost_center"] = p["cost_center"] or payroll_cost_center
                    sal_slip["payroll_cost_center"] = payroll_cost_center # Keep default cost center for reference
                    salary_slips_with_project.append(sal_slip)
            else:
                i["payroll_cost_center"] = payroll_cost_center
                i["cost_center"] = payroll_cost_center # Assign default cost center if not already present
                i["project"] = None
                salary_slips_with_project.append(i)

        return salary_slips_with_project

    def get_net_payable_per_employee(self):
        """
        Calculates the net payable amount for each submitted salary slip.
        Returns a dictionary: {employee_id: net_payable_amount}
        """
        salary_slips = self.get_sal_slip_list(ss_status=1, as_dict=True)
        if not salary_slips:
            return {}

        sal_slip_names = [d.name for d in salary_slips]
        placeholders = ", ".join(["%s"] * len(sal_slip_names))

        # Query net_pay directly from Salary Slip table
        net_pay_data = frappe.db.sql(
            f"""
            SELECT name, employee, net_pay
            FROM `tabSalary Slip`
            WHERE name IN ({placeholders})
            """,
            sal_slip_names,
            as_dict=True,
        )

        return {d.employee: flt(d.net_pay) for d in net_pay_data}

    def make_accrual_jv_entry(self, submitted_salary_slips=None):
        if not self.is_project_payroll_:
            return super().make_accrual_jv_entry(submitted_salary_slips)

        self.check_permission("write")
        
        earnings = self.get_salary_component_total_with_project(component_type="earnings") or {}
        deductions = self.get_salary_component_total_with_project(component_type="deductions") or {}

        payroll_payable_account = self.payroll_payable_account
        jv_name = ""
        precision = frappe.get_precision("Journal Entry Account", "debit_in_account_currency")

        if earnings or deductions:
            journal_entry = frappe.new_doc("Journal Entry")
            journal_entry.voucher_type = "Journal Entry"
            journal_entry.user_remark = _(
                "Accrual Journal Entry for salaries from {0} to {1}"
            ).format(self.start_date, self.end_date)
            journal_entry.company = self.company
            journal_entry.posting_date = self.posting_date
            accounting_dimensions = get_accounting_dimensions() or []

            accounts = []
            currencies = []
            company_currency = get_company_currency(self.company)

            # --- Earnings (Debit Expense Accounts) ---
            for acc_cc, amount in earnings.items():
                if len(acc_cc) == 2:
                    acc_cc += (None,) # Handles cases without a project dimension
                
                (exchange_rate, amt) = self.get_amount_and_exchange_rate_for_journal_entry(
                    acc_cc[0], amount, company_currency, currencies
                )
                
                # Debit line for Earnings/Expense
                accounts.append(
                    self.update_accounting_dimensions(
                        {
                            "account": acc_cc[0],
                            "debit_in_account_currency": flt(amt, precision),
                            "exchange_rate": flt(exchange_rate),
                            "cost_center": acc_cc[2] or self.cost_center,
                            "project": acc_cc[1],
                            "reference_type": "Payroll Entry",
                            "reference_name": self.name,
                            "reference_due_date": self.posting_date,
                        },
                        accounting_dimensions,
                    )
                )

            # --- Deductions (Credit Deduction Liability Accounts) ---
            for acc_cc, amount in deductions.items():
                if len(acc_cc) == 2:
                    acc_cc += (None,) # Handles cases without a project dimension
                
                (exchange_rate, amt) = self.get_amount_and_exchange_rate_for_journal_entry(
                    acc_cc[0], amount, company_currency, currencies
                )
                
                # Credit line for Deductions/Liability
                accounts.append(
                    self.update_accounting_dimensions(
                        {
                            "account": acc_cc[0],
                            "credit_in_account_currency": flt(amt, precision),
                            "exchange_rate": flt(exchange_rate),
                            "cost_center": acc_cc[2] or self.cost_center,
                            "project": acc_cc[1],
                        },
                        accounting_dimensions,
                    )
                )

            # --- Individual Payable Amount (Credit Payroll Payable Account) ---
            net_payables_by_employee = self.get_net_payable_per_employee()
            
            for employee_id, payable_amount in net_payables_by_employee.items():
                if flt(payable_amount) == 0:
                    continue

                (exchange_rate, payable_amt) = self.get_amount_and_exchange_rate_for_journal_entry(
                    payroll_payable_account, payable_amount, company_currency, currencies
                )
                
                # Credit line for Payroll Payable Account - Bifurcated by Employee
                accounts.append(
                    self.update_accounting_dimensions(
                        {
                            "account": payroll_payable_account,
                            "credit_in_account_currency": flt(payable_amt, precision),
                            "exchange_rate": flt(exchange_rate),
                            "cost_center": self.cost_center, # Uses the Payroll Entry's default Cost Center
                            "party_type": "Employee",
                            "party": employee_id,
                            "reference_type": "Payroll Entry",
                            "reference_name": self.name,
                            "reference_due_date": self.posting_date,
                        },
                        accounting_dimensions,
                    )
                )

            # --- Finalize and Submit JV ---
            journal_entry.set("accounts", accounts)
            journal_entry.multi_currency = 1 if len(currencies) > 1 else 0
            journal_entry.title = payroll_payable_account
            
            try:
                journal_entry.insert()
                journal_entry.submit()
                jv_name = journal_entry.name

                if submitted_salary_slips:
                    self.set_journal_entry_in_salary_slips(submitted_salary_slips, jv_name=jv_name)
                else:
                    self.update_salary_slip_status(jv_name=jv_name)
            except Exception as e:
                frappe.log_error(f"Error in make_accrual_jv_entry: {str(e)}")
                frappe.msgprint(_("Error occurred while creating journal entry. Please check error logs."))
                raise

            return jv_name