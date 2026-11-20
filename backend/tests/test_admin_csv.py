import pytest

from estafeta.admin.accounts import CsvError, read_csv
from estafeta.models import Role


def test_csv_utf8_bom_russian_semicolon_and_blank_email() -> None:
    text = (
        "\ufeffФИО;корпоративная почта;отдел;должность;роль\r\n"
        '"Иванов Иван";;ИТ;Специалист;Сотрудник\r\n'
        "Иванова Анна;Anna@ORG.TEST;ИТ;Начальник;Руководитель отдела\r\n"
    )
    first, second = read_csv(text)
    assert first[0] == 2 and first[1].corporate_email is None
    assert second[1].corporate_email == "anna@org.test"
    assert second[1].role is Role.DEPARTMENT_HEAD


def test_csv_english_reordered_columns_and_quoted_comma() -> None:
    [(line, row)] = read_csv(
        'role,full_name,position,department,corporate_email\nemployee,Иван,"ИТ, поддержка",ИТ,'
    )
    assert line == 2 and row.position == "ИТ, поддержка"


def test_csv_collects_row_errors_without_repeating_values() -> None:
    with pytest.raises(CsvError) as error:
        read_csv(
            "full_name,corporate_email,department,position,role\n"
            "Человек,private-invalid-address,Отдел,Оператор,employee\n"
            "Человек,,Отдел,Оператор,unknown-role\n"
        )
    assert [item.row for item in error.value.errors] == [2, 3]
    assert "private-invalid-address" not in str(error.value.errors)
    assert "unknown-role" not in str(error.value.errors)


def test_csv_rejects_oversize_or_too_many_rows() -> None:
    with pytest.raises(CsvError, match="не прошёл"):
        read_csv("а" * 70000)
    with pytest.raises(CsvError) as error:
        read_csv("ФИО;корпоративная почта;отдел;должность;роль\n" + "Иван;;ИТ;Оператор;HR\n" * 301)
    assert error.value.errors[0].row == 302
