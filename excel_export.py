from collections import defaultdict
from datetime import date, datetime
from io import BytesIO

from openpyxl import Workbook
from openpyxl.chart import BarChart, PieChart, Reference
from openpyxl.chart.label import DataLabelList
from openpyxl.formatting.rule import DataBarRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.worksheet.table import Table, TableStyleInfo


GREEN = "245C43"
DARK = "1D2924"
ORANGE = "E16D4C"
GOLD = "E8BE4C"
PALE_GREEN = "E5EFE7"
PALE_ORANGE = "FFF0E9"
PALE_GOLD = "FFF6D9"
WHITE = "FFFFFF"
MUTED = "78827B"
LINE = "E5E9E1"
TABLE_STYLE = "TableStyleMedium4"
MONEY_FORMAT = '#,##0.00 "so\'m"'
DATE_FORMAT = "dd.mm.yyyy"


def _date(value):
    if not value:
        return None
    if isinstance(value, (date, datetime)):
        return value.date() if isinstance(value, datetime) else value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return str(value)


def _text(cell, value):
    cell.value = "" if value is None else str(value)
    cell.data_type = "s"
    cell.alignment = Alignment(vertical="top", wrap_text=True)


def _title(sheet, title, subtitle, last_column):
    sheet.sheet_view.showGridLines = False
    sheet.sheet_properties.pageSetUpPr.fitToPage = True
    sheet.page_setup.orientation = "landscape"
    sheet.page_setup.fitToWidth = 1
    sheet.page_setup.fitToHeight = 0
    sheet.sheet_properties.tabColor = GREEN
    sheet.merge_cells(start_row=1, start_column=1, end_row=1, end_column=last_column)
    cell = sheet.cell(1, 1, title)
    cell.fill = PatternFill("solid", fgColor=GREEN)
    cell.font = Font(name="Aptos Display", size=20, bold=True, color=WHITE)
    cell.alignment = Alignment(vertical="center")
    for column in range(1, last_column + 1):
        sheet.cell(1, column).fill = PatternFill("solid", fgColor=GREEN)
    sheet.row_dimensions[1].height = 38
    sheet.merge_cells(start_row=2, start_column=1, end_row=2, end_column=last_column)
    cell = sheet.cell(2, 1, subtitle)
    cell.font = Font(name="Aptos", size=10, color=MUTED, italic=True)
    cell.alignment = Alignment(vertical="center")
    sheet.row_dimensions[2].height = 24


def _section(sheet, row, start_column, end_column, label):
    sheet.merge_cells(start_row=row, start_column=start_column, end_row=row, end_column=end_column)
    cell = sheet.cell(row, start_column, label)
    cell.fill = PatternFill("solid", fgColor=GREEN)
    cell.font = Font(name="Aptos", size=10, bold=True, color=WHITE)
    cell.alignment = Alignment(vertical="center")
    for column in range(start_column, end_column + 1):
        sheet.cell(row, column).fill = PatternFill("solid", fgColor=GREEN)
    sheet.row_dimensions[row].height = 23


def _card(sheet, start_column, end_column, label, value, fill, number_format=None):
    sheet.merge_cells(start_row=4, start_column=start_column, end_row=4, end_column=end_column)
    sheet.merge_cells(start_row=5, start_column=start_column, end_row=6, end_column=end_column)
    label_cell = sheet.cell(4, start_column, label.upper())
    value_cell = sheet.cell(5, start_column, value)
    for row in (4, 5, 6):
        for column in range(start_column, end_column + 1):
            sheet.cell(row, column).fill = PatternFill("solid", fgColor=fill)
    label_cell.font = Font(name="Aptos", size=9, bold=True, color=MUTED)
    label_cell.alignment = Alignment(horizontal="center", vertical="center")
    value_cell.font = Font(name="Aptos Display", size=17, bold=True, color=DARK)
    value_cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    if number_format:
        value_cell.number_format = number_format
    sheet.row_dimensions[4].height = 21
    sheet.row_dimensions[5].height = 25
    sheet.row_dimensions[6].height = 15


def _write_table(sheet, headers, rows, widths, table_name, date_columns=(), money_columns=(), percent_columns=()):
    header_row = 4
    sheet.sheet_view.showGridLines = False
    sheet.freeze_panes = "A5"
    sheet.sheet_properties.pageSetUpPr.fitToPage = True
    sheet.page_setup.orientation = "landscape"
    sheet.page_setup.fitToWidth = 1
    sheet.page_setup.fitToHeight = 0
    sheet.sheet_properties.tabColor = GREEN
    for column, width in enumerate(widths, start=1):
        sheet.column_dimensions[chr(64 + column)].width = width
        cell = sheet.cell(header_row, column, headers[column - 1])
        cell.fill = PatternFill("solid", fgColor=GREEN)
        cell.font = Font(name="Aptos", size=10, bold=True, color=WHITE)
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    sheet.row_dimensions[header_row].height = 30

    for row_values in rows:
        row_number = sheet.max_row + 1
        for column, value in enumerate(row_values, start=1):
            cell = sheet.cell(row_number, column)
            cell.alignment = Alignment(vertical="top", wrap_text=True)
            if column in date_columns and isinstance(value, date):
                cell.number_format = DATE_FORMAT
            elif column in money_columns and isinstance(value, (int, float)):
                cell.number_format = MONEY_FORMAT
            elif column in percent_columns and isinstance(value, (int, float)):
                cell.number_format = "0%"
            elif isinstance(value, str):
                _text(cell, value)

    if rows:
        table = Table(displayName=table_name, ref=f"A{header_row}:{chr(64 + len(headers))}{sheet.max_row}")
        table.tableStyleInfo = TableStyleInfo(
            name=TABLE_STYLE,
            showFirstColumn=False,
            showLastColumn=False,
            showRowStripes=True,
            showColumnStripes=False,
        )
        sheet.add_table(table)
    else:
        sheet.auto_filter.ref = f"A{header_row}:{chr(64 + len(headers))}{header_row}"
        sheet.cell(header_row + 1, 1, "Hozircha ma'lumot yo'q").font = Font(name="Aptos", italic=True, color=MUTED)
    sheet.sheet_view.zoomScale = 90


def build_excel_report(data, period_label="Shaxsiy reja va moliya hisoboti", group_by_day=False):
    tasks = data.get("tasks", [])
    transactions = data.get("transactions", [])
    budgets = data.get("budgets", [])
    goals = data.get("goals", [])
    reports = data.get("reports", [])

    incomes = sum(float(item["amount"]) for item in transactions if item["type"] == "income")
    expenses = sum(float(item["amount"]) for item in transactions if item["type"] == "expense")
    completed_tasks = sum(int(item.get("is_completed", 0)) for item in tasks)
    net = incomes - expenses

    workbook = Workbook()
    workbook.properties.title = "Turkiston Daily hisoboti"
    workbook.properties.subject = "Kunlik rejalar va shaxsiy moliya"
    workbook.properties.creator = "Turkiston Daily"
    dashboard = workbook.active
    dashboard.title = "Umumiy"
    _title(dashboard, "TURKISTON DAILY", period_label, 8)
    for column in range(1, 9):
        dashboard.column_dimensions[chr(64 + column)].width = 16
    _card(dashboard, 1, 2, "Jami kirim", incomes, PALE_GREEN, MONEY_FORMAT)
    _card(dashboard, 3, 4, "Jami chiqim", expenses, PALE_ORANGE, MONEY_FORMAT)
    _card(dashboard, 5, 6, "Sof balans", net, PALE_GOLD, MONEY_FORMAT)
    _card(dashboard, 7, 8, "Rejalar bajarildi", f"{completed_tasks} / {len(tasks)}", PALE_GREEN)

    timeline = defaultdict(lambda: {"income": 0.0, "expense": 0.0})
    categories = defaultdict(float)
    for item in transactions:
        transaction_date = str(item.get("date", ""))[:10]
        date_group = transaction_date if group_by_day else transaction_date[:7]
        date_group = date_group or "Sana yo'q"
        timeline[date_group][item["type"]] += float(item["amount"])
        if item["type"] == "expense":
            categories[item["category"]] += float(item["amount"])

    _section(dashboard, 8, 1, 4, "DAVR BO'YICHA PUL OQIMI")
    _section(dashboard, 8, 6, 8, "XARAJATLAR KATEGORIYALAR BO'YICHA")
    headers = ["Kun" if group_by_day else "Oy", "Kirim", "Chiqim", "Balans"]
    for column, header in enumerate(headers, 1):
        cell = dashboard.cell(9, column, header)
        cell.fill = PatternFill("solid", fgColor="E5EFE7")
        cell.font = Font(name="Aptos", size=9, bold=True, color=GREEN)
    timeline_rows = []
    for label in sorted(timeline):
        timeline_rows.append((label, timeline[label]["income"], timeline[label]["expense"], timeline[label]["income"] - timeline[label]["expense"]))
    if not timeline_rows:
        timeline_rows.append(("Ma'lumot yo'q", 0, 0, 0))
    for row_number, values in enumerate(timeline_rows, start=10):
        for column, value in enumerate(values, start=1):
            cell = dashboard.cell(row_number, column, value)
            cell.fill = PatternFill("solid", fgColor="F5F7F3" if row_number % 2 == 0 else WHITE)
            cell.font = Font(name="Aptos", size=9, color=DARK)
            if column > 1:
                cell.number_format = MONEY_FORMAT

    category_headers = ("Kategoriya", "Jami chiqim")
    for column, header in enumerate(category_headers, start=6):
        cell = dashboard.cell(9, column, header)
        cell.fill = PatternFill("solid", fgColor="FFF0E9")
        cell.font = Font(name="Aptos", size=9, bold=True, color=ORANGE)
    category_rows = sorted(categories.items(), key=lambda item: item[1], reverse=True)
    if not category_rows:
        category_rows = [("Ma'lumot yo'q", 0)]
    for row_number, (category, amount) in enumerate(category_rows, start=10):
        _text(dashboard.cell(row_number, 6), category)
        dashboard.cell(row_number, 7, amount).number_format = MONEY_FORMAT
        for column in (6, 7):
            dashboard.cell(row_number, column).fill = PatternFill("solid", fgColor="FFF9F6" if row_number % 2 == 0 else WHITE)
            dashboard.cell(row_number, column).font = Font(name="Aptos", size=9, color=DARK)

    if timeline:
        chart = BarChart()
        chart.type = "col"
        chart.style = 10
        chart.title = "Kirim va chiqim"
        chart.y_axis.title = "So'm"
        chart.x_axis.title = "Kun" if group_by_day else "Oy"
        chart.height = 7
        chart.width = 14
        chart.add_data(Reference(dashboard, min_col=2, max_col=3, min_row=9, max_row=9 + len(timeline_rows)), titles_from_data=True)
        chart.set_categories(Reference(dashboard, min_col=1, min_row=10, max_row=9 + len(timeline_rows)))
        chart.legend.position = "b"
        dashboard.add_chart(chart, "A20")
    if categories:
        chart = PieChart()
        chart.title = "Chiqimlar ulushi"
        chart.height = 7
        chart.width = 12
        last_category_row = 9 + len(category_rows)
        chart.add_data(Reference(dashboard, min_col=7, min_row=9, max_row=last_category_row), titles_from_data=True)
        chart.set_categories(Reference(dashboard, min_col=6, min_row=10, max_row=last_category_row))
        chart.dataLabels = DataLabelList()
        chart.dataLabels.showPercent = True
        dashboard.add_chart(chart, "E20")
    dashboard.freeze_panes = "A9"
    dashboard.sheet_view.zoomScale = 90

    transaction_rows = []
    for item in transactions:
        transaction_rows.append([
            _date(item.get("date")),
            "Kirim" if item["type"] == "income" else "Chiqim",
            item.get("category", ""),
            float(item.get("amount", 0)),
            item.get("note", ""),
        ])
    sheet = workbook.create_sheet("Moliya")
    _title(sheet, "KIRIM VA CHIQIMLAR", "Tanlangan hisobot davridagi moliyaviy amaliyotlar", 5)
    _write_table(sheet, ["Sana", "Turi", "Kategoriya", "Summa (so'm)", "Izoh"], transaction_rows, [15, 14, 23, 19, 42], "Transactions", date_columns=(1,), money_columns=(4,))
    for row_number, item in enumerate(transaction_rows, start=5):
        cell = sheet.cell(row_number, 2)
        cell.fill = PatternFill("solid", fgColor=PALE_GREEN if item[1] == "Kirim" else PALE_ORANGE)
        cell.font = Font(name="Aptos", bold=True, color=GREEN if item[1] == "Kirim" else ORANGE)

    task_rows = []
    for item in tasks:
        task_rows.append([
            _date(item.get("date")),
            item.get("title", ""),
            "Bajarildi" if item.get("is_completed") else "Bajarilmagan",
            {"high": "Muhim", "normal": "Oddiy", "low": "Keyinroq"}.get(item.get("priority"), "Oddiy"),
            {"daily": "Har kuni", "weekly": "Har hafta", "monthly": "Har oy"}.get(item.get("recurrence"), "Yo'q"),
            item.get("remind_at") or "",
        ])
    sheet = workbook.create_sheet("Rejalar")
    _title(sheet, "KUNLIK REJALAR", "Bajarilish holati, ustuvorlik va eslatmalar", 6)
    _write_table(sheet, ["Sana", "Reja", "Holati", "Muhimligi", "Takrorlanish", "Eslatma"], task_rows, [15, 42, 18, 15, 18, 22], "Tasks", date_columns=(1,))

    spent_by_budget = defaultdict(float)
    for item in transactions:
        if item["type"] == "expense":
            spent_by_budget[(str(item.get("date", ""))[:7], item.get("category", ""))] += float(item["amount"])
    budget_rows = []
    for item in budgets:
        month = item.get("month", "")
        limit = float(item.get("monthly_limit", 0))
        spent = spent_by_budget[(month, item.get("category", ""))]
        budget_rows.append([month, item.get("category", ""), limit, spent, max(0, limit - spent), spent / limit if limit else 0])
    sheet = workbook.create_sheet("Budjet")
    _title(sheet, "OYLIK BUDJETLAR", "Limitlar va tanlangan hisobot davriga to'g'ri kelgan xarajatlar; qisman oylar ham kiritiladi", 6)
    _write_table(sheet, ["Oy", "Kategoriya", "Limit (so'm)", "Sarflandi (so'm)", "Qoldi (so'm)", "Sarflanish"], budget_rows, [15, 24, 20, 21, 20, 16], "Budgets", money_columns=(3, 4, 5), percent_columns=(6,))
    if budget_rows:
        sheet.conditional_formatting.add(f"F5:F{sheet.max_row}", DataBarRule(start_type="num", start_value=0, end_type="num", end_value=1, color=GREEN, showValue=True))

    goal_rows = []
    for item in goals:
        target = float(item.get("target_amount", 0))
        saved = float(item.get("current_amount", 0))
        goal_rows.append([
            item.get("title", ""),
            target,
            saved,
            max(0, target - saved),
            saved / target if target else 0,
            _date(item.get("deadline")),
            "Bajarildi" if item.get("is_completed") else "Jarayonda",
        ])
    sheet = workbook.create_sheet("Jamg'arma")
    _title(sheet, "JAMG'ARMA MAQSADLARI", "Maqsadlar sari to'plangan mablag'", 7)
    _write_table(sheet, ["Maqsad", "Maqsad (so'm)", "Yig'ildi (so'm)", "Qoldi (so'm)", "Bajarildi", "Muddat", "Holati"], goal_rows, [32, 20, 20, 20, 14, 16, 16], "Goals", money_columns=(2, 3, 4), percent_columns=(5,), date_columns=(6,))
    if goal_rows:
        sheet.conditional_formatting.add(f"E5:E{sheet.max_row}", DataBarRule(start_type="num", start_value=0, end_type="num", end_value=1, color=ORANGE, showValue=True))

    report_rows = []
    for item in reports:
        report_rows.append([
            _date(item.get("date")),
            int(item.get("score", 0)),
            int(item.get("tasks_completed", 0)),
            int(item.get("tasks_total", 0)),
            float(item.get("income", 0)),
            float(item.get("expense", 0)),
            float(item.get("income", 0)) - float(item.get("expense", 0)),
            item.get("note", ""),
        ])
    sheet = workbook.create_sheet("Kunlik hisobot")
    _title(sheet, "KUNLIK HISOBOTLAR", "Kun yakuni, kayfiyat bahosi va moliyaviy xulosa", 8)
    _write_table(sheet, ["Sana", "Baho / 5", "Bajarildi", "Jami reja", "Kirim (so'm)", "Chiqim (so'm)", "Balans (so'm)", "Kun yakuni"], report_rows, [15, 12, 14, 14, 19, 19, 19, 48], "Reports", date_columns=(1,), money_columns=(5, 6, 7))

    workbook.active = 0
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()