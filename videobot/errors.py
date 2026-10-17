class JobError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


ERRORS = {
    "unsafe": "Этот адрес не подходит. Пришлите ссылку на общедоступную страницу с видео.",
    "size": "Видео превышает лимит загрузки бота. Выберите меньшее качество: /quality.",
    "duration": "Видео слишком длинное для текущих настроек бота.",
    "live": "Прямые эфиры не поддерживаются. Пришлите ссылку на готовую запись.",
    "playlist": "Нужна ссылка на один ролик, а не на плейлист, канал или подборку.",
    "auth": "Сайт требует вход в аккаунт. Владелец бота может добавить cookies в настройках.",
    "drm": "Это видео защищено DRM; бот не может его скачать.",
    "unavailable": "Видео удалено, закрыто или недоступно из региона сервера.",
    "unsupported": "Не удалось извлечь видео с этой страницы. Попробуйте прямую ссылку на ролик.",
    "network": "Сайт не ответил или ограничил загрузку. Попробуйте позже.",
    "parts": "Для этого ролика нужно слишком много частей. Выберите меньшее качество: /quality.",
    "processing": "Не удалось подготовить видео. Попробуйте другое качество: /quality.",
    "timeout": "Превышено время обработки. Попробуйте более короткое видео или меньшее качество.",
    "storage": "На сервере недостаточно свободного места. Сообщите владельцу бота.",
    "internal": "Не удалось обработать видео. Попробуйте другую ссылку или повторите позже.",
}


def classify_error(message: str) -> str:
    text = message.lower()
    if any(word in text for word in ("drm", "protected content")):
        return "drm"
    if any(word in text for word in ("sign in", "login", "log in", "cookies", "authentication", "private video")):
        return "auth"
    if any(word in text for word in ("unsupported url", "no video", "no formats", "requested format is not available")):
        return "unsupported"
    if any(word in text for word in ("geo", "not available", "unavailable", "removed", "404", "403")):
        return "unavailable"
    if any(word in text for word in ("timed out", "timeout", "429", "connection", "resolve", "network")):
        return "network"
    if "file is larger" in text or "max-filesize" in text:
        return "size"
    return "internal"
