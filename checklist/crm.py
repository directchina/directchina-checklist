from .core import CheckError
from .session import Cabinet

NEWS = "/_module/system/rest/announcement/state"


def run(report, config, now):
    with Cabinet("https://e-x1.aspro.cloud", [NEWS]) as client:
        client.login_crm()

        def news():
            data = client.json(NEWS)
            if data.get("result") is not True or not isinstance(
                data.get("has_unread"), bool
            ):
                raise CheckError("Неожиданный формат состояния новостей")
            report.add(
                "CRM",
                "Системные новости",
                "Есть непрочитанные объявления сервиса"
                if data["has_unread"]
                else "Непрочитанных объявлений сервиса нет",
                "WARN" if data["has_unread"] else "OK",
            )

        report.attempt("CRM", "Системные новости", news)
