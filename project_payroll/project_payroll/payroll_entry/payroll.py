
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
                select
                    ssd.salary_component,
                    ssd.amount,
                    ssd.parentfield,
                    ssd.do_not_include_in_accounts,
                    ss.employee,
                    ss.start_date,
                    ss.end_date
                from `tabSalary Slip` ss, `tabSalary Detail` ssd
                where ss.name = ssd.parent
                    and ssd.parentfield = %s
                    and ss.name in ({placeholders})
                    and ss.payroll_entry = %s
                    and (
                        ifnull(ssd.do_not_include_in_total, 0) = 0
                        or (
                            ifnull(ssd.do_not_include_in_total, 0) = 1
                            and ifnull(ssd.do_not_include_in_accounts, 0) = 0
                        )
                    )
            """,
                [component_type] + salary_slips_names + [self.name],
                as_dict=True,
            )
            return self.set_employee_ammount_with_project_account_dimention(
                salary_components
            )

    def get_account(self, component_dict=None):
        if not self.is_project_payroll_:
            return super().get_account()
        account_dict = {}
        for key, amount in component_dict.items():
            account = self.get_salary_component_account(key[0])
            account_dict[(account, key[1], key[2])] = account_dict.get((account, key[1], key[2]), 0) + amount
        return account_dict

    def get_salary_component_total_with_project(self, component_type, employee_wise_accounting_enabled=False):
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
                        key = (item.salary_component, item.project, cost_center)
                    else:
                        key = (item.salary_component, None, cost_center)

                    component_dict[key] = component_dict.get(key, 0) + flt(item.amount)
                    
                    if employee_wise_accounting_enabled:
                        self.set_employee_based_payroll_payable_entries(
                            component_type, item["employee"], flt(item.amount)
                        )

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
                    sal_slip["cost_center"] = p["cost_center"] or payroll_cost_center
                    sal_slip["payroll_cost_center"] = payroll_cost_center
                    salary_slips_with_project.append(sal_slip)
            else:
                i["payroll_cost_center"] = payroll_cost_center
                i["cost_center"] = payroll_cost_center
                i["project"] = None
                salary_slips_with_project.append(i)

        return salary_slips_with_project

    def set_employee_based_payroll_payable_entries(
        self, component_type, employee, amount, salary_structure=None
    ):
        """
        Track employee-wise payroll payable entries for employee-based accounting
        """
        employee_details = self.employee_based_payroll_payable_entries.setdefault(employee, {})

        employee_details.setdefault(component_type, 0)
        employee_details[component_type] += amount

        if salary_structure and "salary_structure" not in employee_details:
            employee_details["salary_structure"] = salary_structure

    def get_employee_project_allocation(self, employee):
        """
        Get project allocation for a specific employee during the payroll period
        """
        employee_project = frappe.get_all(
            "Employee Projects Payroll",
            filters={
                "docstatus": 1,
                "employee": employee,
                "from_date": ["<=", self.start_date],
                "to_date": [">=", self.end_date],
            },
            fields=["name"],
        )
        
        if employee_project:
            projects = frappe.get_all(
                "Employee Project",
                filters={"parent": employee_project[0]["name"]},
                fields=["project", "cost_center", "percent_pay"],
            )
            return projects
        return []

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
            AND payroll_entry = %s
            """,
            sal_slip_names + [self.name],
            as_dict=True,
        )

        return {d.employee: flt(d.net_pay) for d in net_pay_data}

    def debug_salary_slips(self):
        """Debug method to check salary slip retrieval"""
        # Check all salary slips for this payroll entry
        all_salary_slips = frappe.db.sql(
            """
            SELECT name, employee, docstatus, payroll_entry, net_pay
            FROM `tabSalary Slip`
            WHERE payroll_entry = %s
            ORDER BY employee
            """,
            [self.name],
            as_dict=True,
        )
        
        # Check salary slips that should be submitted (docstatus = 0)
        draft_salary_slips = frappe.db.sql(
            """
            SELECT name, employee, docstatus, payroll_entry, net_pay
            FROM `tabSalary Slip`
            WHERE payroll_entry = %s AND docstatus = 0
            ORDER BY employee
            """,
            [self.name],
            as_dict=True,
        )
        
        # Check what get_sal_slip_list returns
        hrms_salary_slips = self.get_sal_slip_list(ss_status=0, as_dict=True)
        
        frappe.msgprint(f"""
        <b>Debug Information:</b><br>
        Total Salary Slips: {len(all_salary_slips)}<br>
        Draft Salary Slips: {len(draft_salary_slips)}<br>
        HRMS get_sal_slip_list returns: {len(hrms_salary_slips)}<br>
        <br>
        <b>All Salary Slips:</b><br>
        {', '.join([f"{ss.employee} ({ss.name}) - Status: {ss.docstatus}" for ss in all_salary_slips])}<br>
        <br>
        <b>Draft Salary Slips:</b><br>
        {', '.join([f"{ss.employee} ({ss.name})" for ss in draft_salary_slips])}<br>
        <br>
        <b>HRMS get_sal_slip_list (ss_status=0):</b><br>
        {', '.join([f"{ss.employee} ({ss.name})" for ss in hrms_salary_slips])}
        """)

    def debug_employee_wise_accounting(self):
        """Debug employee-wise accounting entries"""
        employee_wise_accounting_enabled = frappe.db.get_single_value(
            "Payroll Settings", "process_payroll_accounting_entry_based_on_employee"
        )
        
        if not employee_wise_accounting_enabled:
            frappe.msgprint("Employee-wise accounting is DISABLED")
            return
            
        # Get earnings and deductions to populate employee_based_payroll_payable_entries
        earnings = self.get_salary_component_total_with_project(
            component_type="earnings", 
            employee_wise_accounting_enabled=True
        ) or {}
        
        deductions = self.get_salary_component_total_with_project(
            component_type="deductions", 
            employee_wise_accounting_enabled=True
        ) or {}
        
        frappe.msgprint(f"""
        <b>Employee-wise Accounting Debug:</b><br>
        Setting Enabled: {employee_wise_accounting_enabled}<br>
        <br>
        <b>Employee Based Payroll Payable Entries:</b><br>
        {self.employee_based_payroll_payable_entries}<br>
        <br>
        <b>Earnings Total:</b> {sum(earnings.values())}<br>
        <b>Deductions Total:</b> {sum(deductions.values())}<br>
        <br>
        <b>Earnings:</b><br>
        {earnings}<br>
        <br>
        <b>Deductions:</b><br>
        {deductions}
        """)

    def fix_salary_slip_payroll_entry(self):
        """Fix salary slips that don't have payroll_entry set"""
        # Find salary slips for this period that don't have payroll_entry set
        salary_slips_to_fix = frappe.db.sql(
            """
            SELECT name, employee, start_date, end_date
            FROM `tabSalary Slip`
            WHERE start_date >= %s 
            AND end_date <= %s 
            AND (payroll_entry IS NULL OR payroll_entry = '')
            AND docstatus = 0
            ORDER BY employee
            """,
            [self.start_date, self.end_date],
            as_dict=True,
        )
        
        if salary_slips_to_fix:
            # Update salary slips to set payroll_entry
            for ss in salary_slips_to_fix:
                frappe.db.set_value("Salary Slip", ss.name, "payroll_entry", self.name)
            
            frappe.db.commit()
            
            frappe.msgprint(f"""
            <b>Fixed {len(salary_slips_to_fix)} salary slips:</b><br>
            {', '.join([f"{ss.employee} ({ss.name})" for ss in salary_slips_to_fix])}<br>
            <br>
            Now try submitting salary slips again.
            """)
        else:
            frappe.msgprint("No salary slips found that need fixing.")

    def make_accrual_jv_entry(self, submitted_salary_slips=None):
        if not self.is_project_payroll_:
            return super().make_accrual_jv_entry(submitted_salary_slips)

        self.check_permission("write")
        # Get accounting settings and data
        employee_wise_accounting_enabled = self._get_employee_wise_accounting_setting()
        earnings, deductions = self._get_salary_components(employee_wise_accounting_enabled)
        
        if not (earnings or deductions):
            return ""

        # Create journal entry
        journal_entry = self._create_journal_entry(employee_wise_accounting_enabled)
        accounts = self._build_journal_entry_accounts(
            earnings, deductions, employee_wise_accounting_enabled
        )
        # Submit journal entry
        return self._submit_journal_entry(journal_entry, accounts, submitted_salary_slips)

    def _get_employee_wise_accounting_setting(self):
        """Get employee-wise accounting setting from Payroll Settings"""
        employee_wise_accounting_enabled = frappe.db.get_single_value(
            "Payroll Settings", "process_payroll_accounting_entry_based_on_employee"
        )
        # frappe.throw(str(employee_wise_accounting_enabled))
        if employee_wise_accounting_enabled:
            self.employee_based_payroll_payable_entries = {}
            
        return employee_wise_accounting_enabled

    def _get_salary_components(self, employee_wise_accounting_enabled):
        """Get earnings and deductions with project allocation"""
        earnings = self.get_salary_component_total_with_project(
            component_type="earnings", 
            employee_wise_accounting_enabled=employee_wise_accounting_enabled
        ) or {}
        
        deductions = self.get_salary_component_total_with_project(
            component_type="deductions", 
            employee_wise_accounting_enabled=employee_wise_accounting_enabled
        ) or {}
        
        return earnings, deductions

    def _create_journal_entry(self, employee_wise_accounting_enabled):
        """Create and configure journal entry"""
        journal_entry = frappe.new_doc("Journal Entry")
        journal_entry.voucher_type = "Journal Entry"
        journal_entry.user_remark = _(
            "Accrual Journal Entry for salaries from {0} to {1}"
        ).format(self.start_date, self.end_date)
        journal_entry.company = self.company
        journal_entry.posting_date = self.posting_date
        journal_entry.title = self.payroll_payable_account
        # Set party_not_required flag to skip party validation when employee-wise accounting is disabled
        # This matches HRMS behavior - when employee-wise is disabled, party is not required
        journal_entry.party_not_required = True if not employee_wise_accounting_enabled else False
        
        return journal_entry

    def _build_journal_entry_accounts(self, earnings, deductions, employee_wise_accounting_enabled):
        """Build all journal entry accounts"""
        accounts = []
        currencies = []
        company_currency = get_company_currency(self.company)
        accounting_dimensions = get_accounting_dimensions() or []
        precision = frappe.get_precision("Journal Entry Account", "debit_in_account_currency")

        # Add earnings accounts
        accounts.extend(self._build_earnings_accounts(earnings, currencies, company_currency, accounting_dimensions, precision))
        
        # Add deductions accounts
        accounts.extend(self._build_deductions_accounts(deductions, currencies, company_currency, accounting_dimensions, precision))
        
        # Add payable accounts
        accounts.extend(self._build_payable_accounts(employee_wise_accounting_enabled, currencies, company_currency, accounting_dimensions, precision, earnings, deductions))

        return accounts

    def _build_earnings_accounts(self, earnings, currencies, company_currency, accounting_dimensions, precision):
        """Build earnings (debit) accounts"""
        accounts = []
        
        for acc_cc, amount in earnings.items():
            if len(acc_cc) == 2:
                acc_cc += (None,)  
            
            exchange_rate, amt = self.get_amount_and_exchange_rate_for_journal_entry(
                acc_cc[0], amount, company_currency, currencies
            )
            
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
        
        return accounts

    def _build_deductions_accounts(self, deductions, currencies, company_currency, accounting_dimensions, precision):
        """Build deductions (credit) accounts"""
        accounts = []
        
        for acc_cc, amount in deductions.items():
            if len(acc_cc) == 2:
                acc_cc += (None,)  
            
            exchange_rate, amt = self.get_amount_and_exchange_rate_for_journal_entry(
                acc_cc[0], amount, company_currency, currencies
            )
            
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
        
        return accounts

    def _build_payable_accounts(self, employee_wise_accounting_enabled, currencies, company_currency, accounting_dimensions, precision, earnings=None, deductions=None):
        """Build payable (credit) accounts"""
        accounts = []
        
        if employee_wise_accounting_enabled:
            accounts.extend(self._build_employee_wise_payable_accounts(currencies, company_currency, accounting_dimensions, precision))
        else:
            accounts.extend(self._build_standard_payable_accounts(currencies, company_currency, accounting_dimensions, precision, earnings, deductions))
        
        return accounts

    def _build_employee_wise_payable_accounts(self, currencies, company_currency, accounting_dimensions, precision):
        """Build employee-wise payable accounts"""
        accounts = []
        
        for employee, employee_details in self.employee_based_payroll_payable_entries.items():
            payable_amount = (employee_details.get("earnings", 0) or 0) - (
                employee_details.get("deductions", 0) or 0
            )
            
            if flt(payable_amount) == 0:
                continue

            accounts.extend(
                self._create_employee_payable_accounts(
                    employee, payable_amount, currencies, company_currency, accounting_dimensions, precision
                )
            )
        
        return accounts

    def _build_standard_payable_accounts(self, currencies, company_currency, accounting_dimensions, precision, earnings=None, deductions=None):
        """Build standard payable accounts - single total payable line"""
        accounts = []
        
        # Use provided earnings and deductions, or calculate if not provided
        if earnings is None:
            earnings = self.get_salary_component_total_with_project(component_type="earnings") or {}
        if deductions is None:
            deductions = self.get_salary_component_total_with_project(component_type="deductions") or {}
            
        total_earnings = sum(amount for amount in earnings.values())
        total_deductions = sum(amount for amount in deductions.values())
        total_payable = total_earnings - total_deductions
        
        if flt(total_payable) == 0:
            return accounts
        
        # Create single payable account entry (not split by employee)
        exchange_rate, payable_amt = self.get_amount_and_exchange_rate_for_journal_entry(
            self.payroll_payable_account, total_payable, company_currency, currencies
        )
        
        accounts.append(
            self.update_accounting_dimensions(
                {
                    "account": self.payroll_payable_account,
                    "credit_in_account_currency": flt(payable_amt, precision),
                    "exchange_rate": flt(exchange_rate),
                    "cost_center": self.cost_center,
                    "reference_type": "Payroll Entry",
                    "reference_name": self.name,
                    "reference_due_date": self.posting_date,
                },
                accounting_dimensions,
            )
        )
        
        return accounts

    def _create_employee_payable_accounts(self, employee, payable_amount, currencies, company_currency, accounting_dimensions, precision):
        """Create payable accounts for a specific employee - one line per employee with total amount"""
        accounts = []
        
        # For employee-wise accounting, create ONE payable line per employee with total amount
        # The project allocation is handled in the earnings/deductions (debit) side
        accounts.append(
            self._create_payable_account_entry(
                employee, payable_amount, None, currencies, company_currency, accounting_dimensions, precision
            )
        )
        
        return accounts

    def _create_payable_account_entry(self, employee, amount, project_info, currencies, company_currency, accounting_dimensions, precision):
        """Create a single payable account entry"""
        exchange_rate, payable_amt = self.get_amount_and_exchange_rate_for_journal_entry(
            self.payroll_payable_account, amount, company_currency, currencies
        )
        
        account_data = {
            "account": self.payroll_payable_account,
            "credit_in_account_currency": flt(payable_amt, precision),
            "exchange_rate": flt(exchange_rate),
            "party_type": "Employee",
            "party": employee,
            "reference_type": "Payroll Entry",
            "reference_name": self.name,
            "reference_due_date": self.posting_date,
            "cost_center": self.cost_center,  
        }
        
        # Only add project dimension if project_info is provided
        # For employee-wise accounting, we don't split payable by project
        if project_info:
            account_data.update({
                "cost_center": project_info["cost_center"] or self.cost_center,
                "project": project_info["project"],
            })
        
        return self.update_accounting_dimensions(account_data, accounting_dimensions)

    def _submit_journal_entry(self, journal_entry, accounts, submitted_salary_slips):
        """Submit the journal entry and update salary slip status"""
        journal_entry.set("accounts", accounts)
        journal_entry.multi_currency = 1 if len(set(acc.get("exchange_rate", 1) for acc in accounts)) > 1 else 0
        
        try:
            journal_entry.insert()
            journal_entry.submit()
            jv_name = journal_entry.name

            if submitted_salary_slips:
                self.set_journal_entry_in_salary_slips(submitted_salary_slips, jv_name=jv_name)
            else:
                self.update_salary_slip_status(jv_name=jv_name)
                
            return jv_name
            
        except Exception as e:
            frappe.log_error(f"Error in make_accrual_jv_entry: {str(e)}")
            frappe.msgprint(_("Error occurred while creating journal entry. Please check error logs."))
            raise
