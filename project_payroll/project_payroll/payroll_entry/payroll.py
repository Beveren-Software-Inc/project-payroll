
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
                        # Key: (Salary Component, Project, Cost Center)
                        key = (item.salary_component, item.project, cost_center)
                    else:
                        # Key: (Salary Component, None, Cost Center)
                        key = (item.salary_component, None, cost_center)

                    component_dict[key] = component_dict.get(key, 0) + flt(item.amount)
                    
                    # Track employee-wise accounting if enabled
                    if employee_wise_accounting_enabled:
                        self.set_employee_based_payroll_payable_entries(
                            component_type, item["employee"], flt(item.amount)
                        )

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
            """,
            sal_slip_names,
            as_dict=True,
        )

        return {d.employee: flt(d.net_pay) for d in net_pay_data}

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
        journal_entry = self._create_journal_entry()
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

    def _create_journal_entry(self):
        """Create and configure journal entry"""
        journal_entry = frappe.new_doc("Journal Entry")
        journal_entry.voucher_type = "Journal Entry"
        journal_entry.user_remark = _(
            "Accrual Journal Entry for salaries from {0} to {1}"
        ).format(self.start_date, self.end_date)
        journal_entry.company = self.company
        journal_entry.posting_date = self.posting_date
        journal_entry.title = self.payroll_payable_account
        
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
        accounts.extend(self._build_payable_accounts(employee_wise_accounting_enabled, currencies, company_currency, accounting_dimensions, precision))

        return accounts

    def _build_earnings_accounts(self, earnings, currencies, company_currency, accounting_dimensions, precision):
        """Build earnings (debit) accounts"""
        accounts = []
        
        for acc_cc, amount in earnings.items():
            if len(acc_cc) == 2:
                acc_cc += (None,)  # Handle cases without project dimension
            
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
                acc_cc += (None,)  # Handle cases without project dimension
            
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

    def _build_payable_accounts(self, employee_wise_accounting_enabled, currencies, company_currency, accounting_dimensions, precision):
        """Build payable (credit) accounts"""
        accounts = []
        
        if employee_wise_accounting_enabled:
            accounts.extend(self._build_employee_wise_payable_accounts(currencies, company_currency, accounting_dimensions, precision))
        else:
            accounts.extend(self._build_standard_payable_accounts(currencies, company_currency, accounting_dimensions, precision))
        
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

    def _build_standard_payable_accounts(self, currencies, company_currency, accounting_dimensions, precision):
        """Build standard payable accounts"""
        accounts = []
        net_payables_by_employee = self.get_net_payable_per_employee()
        
        for employee_id, payable_amount in net_payables_by_employee.items():
            if flt(payable_amount) == 0:
                continue

            accounts.extend(
                self._create_employee_payable_accounts(
                    employee_id, payable_amount, currencies, company_currency, accounting_dimensions, precision
                )
            )
        
        return accounts

    def _create_employee_payable_accounts(self, employee, payable_amount, currencies, company_currency, accounting_dimensions, precision):
        """Create payable accounts for a specific employee with project allocation"""
        accounts = []
        employee_projects = self.get_employee_project_allocation(employee)
        
        if employee_projects:
            # Split payable amount across projects
            for project_info in employee_projects:
                project_payable_amount = payable_amount * (project_info["percent_pay"] / 100)
                
                if flt(project_payable_amount) == 0:
                    continue
                
                accounts.append(
                    self._create_payable_account_entry(
                        employee, project_payable_amount, project_info, currencies, company_currency, accounting_dimensions, precision
                    )
                )
        else:
            # No project allocation - use default cost center
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
        }
        
        if project_info:
            account_data.update({
                "cost_center": project_info["cost_center"] or self.cost_center,
                "project": project_info["project"],
            })
        else:
            account_data["cost_center"] = self.cost_center
        
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
