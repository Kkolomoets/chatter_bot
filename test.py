import requests
import json
from datetime import datetime, timedelta
import time, random

Bearer = "eyJhbGciOiJIUzUxMiJ9.eyJpc3MiOiJtY3MtaWRlbnRpdHkiLCJzdWIiOiJPUEVSQVRPUjc2MFNAZ21haWwuY29tIiwiYWdlbmN5SWQiOiJhN2RmYWVjMC1mOTFjLTRmMWItYjlkNC1iZjVlNmVjMjc3OGMiLCJ1c2VySWQiOiIyZDY1NmMxZS1hOGY0LTQzZmQtYjUwZi1mMzQ3NjMyNDEyMGMiLCJleHBpcmF0aW9uRGF0ZSI6MTc3MjU1Nzk5Nn0.8w2wnEXybip3mnKSeOJvTSZSntcxdajulUAJiwvCXezdPC_ywPum1Zt1mzZrZAKBHdY9EmeIFbpXAcHTWYzbQA"

headers = {
    "Accept": "application/json, text/plain, */*",
    "Authorization": f"Bearer {Bearer}",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
}


# проверить уведомления
def check_unanswered(debug=True):
    messages = 0
    if debug:
        with open("unanswered.json", "r", encoding="UTF-8") as json_data:
            data = json.load(json_data)
    # else:
    #     data = get_unanswered()

    users = data

    for user in users:
        if user:
            if (limit := get_limits(user["profileId"], user["customer"]["id"])) > 0:
                print(limit)
                messages += 1
        time.sleep(random.randint(1, 2))
    return messages


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


# result = get_limits(123599386, 148730613, debug=True)
# print(result)
print(check_unanswered())
