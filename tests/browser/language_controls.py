"""Exercise the language menu, or the live-rebinding API inside modal tests."""

from playwright.sync_api import Page


def choose_language(page: Page, language: str) -> None:
    page.locator("#language-switcher").click()
    page.locator(f'#language-menu [data-language="{language}"]').click()


def rebind_language(page: Page, language: str) -> None:
    # A modal dialog intentionally makes the header controls inert.
    page.evaluate(
        """async language => {
            const url = document.querySelector('link[href$="/app.css"]').href;
            const {setLanguage} = await import(new URL('i18n.js', url).href);
            setLanguage(language);
        }""",
        language,
    )
