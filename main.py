import requests
import json
from datetime import datetime, timedelta
import time, random

# Bearer
choise = int(input("Чей аккаунт проверять: Руслана - 1, Софии - 2: "))
if choise == 1:
    Bearer = "eyJhbGciOiJIUzUxMiJ9.eyJpc3MiOiJtY3MtaWRlbnRpdHkiLCJzdWIiOiJPUEVSQVRPUjYyM01AZ21haWwuY29tIiwiYWdlbmN5SWQiOiJhN2RmYWVjMC1mOTFjLTRmMWItYjlkNC1iZjVlNmVjMjc3OGMiLCJ1c2VySWQiOiIzYWE4YjcwMy04ZDZkLTQ2ZTctODc4Yy03ZDNiZjQ5Nzk4NWYiLCJleHBpcmF0aW9uRGF0ZSI6MTc3MjU0NjIyM30.Zmw364vg5ldVIm3LPuGR7taruDEwNX5xrgeH6qwc6oz3UC3SRu-bo58r9o5uYP3EDn1VeMTeQOzrxgd4s26hig"
elif choise == 2:
    Bearer = "eyJhbGciOiJIUzUxMiJ9.eyJpc3MiOiJtY3MtaWRlbnRpdHkiLCJzdWIiOiJPUEVSQVRPUjc2MFNAZ21haWwuY29tIiwiYWdlbmN5SWQiOiJhN2RmYWVjMC1mOTFjLTRmMWItYjlkNC1iZjVlNmVjMjc3OGMiLCJ1c2VySWQiOiIyZDY1NmMxZS1hOGY0LTQzZmQtYjUwZi1mMzQ3NjMyNDEyMGMiLCJleHBpcmF0aW9uRGF0ZSI6MTc3MjU1Nzk5Nn0.8w2wnEXybip3mnKSeOJvTSZSntcxdajulUAJiwvCXezdPC_ywPum1Zt1mzZrZAKBHdY9EmeIFbpXAcHTWYzbQA"

headers = {
    "Accept": "application/json, text/plain, */*",
    "Authorization": f"Bearer {Bearer}",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
}


# получить активных клиентов
def get_users_response(girl_account_id, online="true", headers=headers):
    url = f"https://mcs-1.chat-space.ai:8001/operator/chat?profileId=pd-{girl_account_id}&criteria=PD_ACTIVE&cursor=&online={online}"
    response = requests.get(url, headers=headers)

    if response.status_code != 200:
        print("Ошибка get_users:", response.status_code)
        exit()

    data = response.json()
    if online:
        with open("users_response.json", "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    else:
        with open("offline_users_response.json", "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    return data


# обработать активных клиентов
def get_users(girl_account_id, debug=False) -> list[dict]:
    # def
    users_online = []
    # online
    if debug:
        with open("users_response.json", "r", encoding="UTF-8") as json_data:
            data = json.load(json_data)
    else:
        data = get_users_response(girl_account_id)

    users = data.get("dialogs", [])

    for user in users:
        # Find the user with last message more than 1 hour ago
        if (
            datetime.now()
            - datetime.strptime(user["createdDate"], "%Y-%m-%dT%H:%M:%SZ")
            > timedelta(hours=2)
            and user["messagesLeft"] > 0
        ):
            users_online.append(
                {
                    "user_name": user["customer"]["name"],
                    "user_id": user["customer"]["id"],
                    "girl_id": user["profileId"].replace("pd-", ""),
                    "messagesLeft": user["messagesLeft"],
                    "status": user["highlightType"],
                }
            )

    # offline
    if debug:
        with open("offline_users_response.json", "r", encoding="UTF-8") as json_data:
            data = json.load(json_data)
    else:
        data = get_users_response(girl_account_id, online=False)

    users = data.get("dialogs", [])

    for user in users:
        # Find the user with an exclamation mark
        if user["highlightType"] == "unanswered":
            users_online.append(
                {
                    "user_name": user["customer"]["name"],
                    "user_id": user["customer"]["id"],
                    "girl_id": user["profileId"].replace("pd-", ""),
                    "messagesLeft": user["messagesLeft"],
                    "status": user["highlightType"],
                }
            )
    return users_online


# получить анкеты в работе
def get_girl_ids(debug=False, headers=headers):
    url = "https://mcs-1.chat-space.ai:8001/identity/cabinets/assigned"

    list_of_id = []
    name_id = {}

    if debug:
        with open("girl_id.json") as json_data:
            data = json.load(json_data)
    else:
        response = requests.get(url, headers=headers)

        if response.status_code != 200:
            print("Ошибка get_girl_id:", response.status_code)
            exit()

        data = response.json()

        with open("girl_id.json", "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    for girl in data:
        girl_id, girl_name = girl["name"].split(" ")
        list_of_id.append(girl_id)
        name_id[girl_id] = girl_name

    return list_of_id, name_id


# получить уведомления
def get_unanswered(headers=headers):
    url = "https://mcs-1.chat-space.ai:8001/operator/chat/unanswered?contentTypes=AUDIO,COMMENT,VIRTUAL_GIFT_BATCH,PHOTO_BATCH,MESSAGE,HTML,PHOTO,STICKER,VIDEO,REAL_PRESENT,TEXT_WITH_PHOTO_CONTENT,LIKE_USER,WINK,LIKE_PHOTO,LIKE_NEWSFEED_POST,REPLY_NEWSFEED_POST"

    response = requests.get(url, headers=headers)

    if response.status_code != 200:
        print("Ошибка get_unanswered:", response.status_code)
        exit()

    data = response.json()
    with open("unanswered.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    return data


# проверить уведомления
def check_unanswered(debug=False):
    messages = 0
    if debug:
        with open("unanswered.json", "r", encoding="UTF-8") as json_data:
            data = json.load(json_data)
    else:
        data = get_unanswered()

    users = data

    for user in users:
        if user:
            if get_limits(user["profileId"], user["customer"]["id"]) > 0:
                messages += 1
        time.sleep(random.randint(1, 2))
    return messages


# проверка лимитов
def get_limits(girl_id, customer_id, headers=headers, debug=False):
    url = f"https://mcs-1.chat-space.ai:8001/operator/chat/restriction?profileId={girl_id}&customerId={customer_id}"
    if debug:
        with open("get_limits.json", "r", encoding="UTF-8") as json_data:
            data = json.load(json_data)
    else:
        response = requests.get(url, headers=headers)
        if response.status_code != 200:
            print("Ошибка get_limits:", response.status_code)
            exit()

        data = response.json()
        with open("get_limits.json", "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    return data["messagesLeft"]


# вызов программы
def main(debug=False):
    # debug
    list_of_id, name_id = get_girl_ids(debug=debug)
    amount_of_account = len(list_of_id)

    print(f"Профиля в проверке: {amount_of_account}")
    for id, name in name_id.items():
        print(f"{name}: {id}")

    next_check = {id: time.monotonic() + random.randint(0, 20) for id in list_of_id}

    message_interval = random.randint(80, 95)
    next_message_check = time.monotonic() + message_interval

    while True:
        now = time.monotonic()

        for id in list_of_id:
            if now >= next_check[id]:

                # debug
                users = get_users(id, debug=debug)

                for user in users:
                    print(
                        f"Имя профиля: {user['user_name']}, имя анкеты: {name_id[user['girl_id']]}, доступные сообщений: {user['messagesLeft']}",
                        ", Важный!!!" if user["status"] == "unanswered" else "",
                    )

                next_check[id] = now + random.randint(130, 270)

        if now >= next_message_check:
            messages = check_unanswered()

            if messages != 0:
                print(f"Непрочитанные сообщения: {messages}")

            next_message_check += message_interval

        time.sleep(1)  # защита от 100% загрузки CPU


main(debug=False)
