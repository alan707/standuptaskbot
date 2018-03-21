#!/usr/bin/python3

import os
import time
import re
from slackclient import SlackClient
import config
from task_manager import TaskList, TaskManager, Status

# instantiate Slack client
slack_client = SlackClient(config.auth["bot_access_token"])
# starterbot's user ID in Slack: value is assigned after the bot starts up
starterbot_id = None
taskmanager = TaskManager()

RECONNECT_DELAY = 10
HIGH_ACTIVITY_WINDOW_SEC = 30
HIGH_ACTIVITY_DELAY_S = 0.1
LOW_ACTIVITY_DELAY_S = 1.0

# constants
kPublishToChannel = config.general["post_to_channel"]

class Bot:
    def __init__(self):
        self.user_list = {}
        self.conversations = {}
        self.channels = {}
        self.high_activity_mode = False
        self.last_activity_ts = time.time()

    def mainloop(self):
        while (True):
            if slack_client.rtm_connect(with_team_state=False):
                print("Starter Bot connected and running!")
                # Read bot's user ID by calling Web API method `auth.test`
                starterbot_id = slack_client.api_call("auth.test")["user_id"]
                users = slack_client.api_call("users.list")["members"]
                channels = slack_client.api_call("channels.list")["channels"]

                for user in users:
                    self.user_list[user["id"]] = user

                for channel in channels:
                    self.channels[channel["name"]] = channel["id"]

                while True:
                    try:
                        if self.parse_bot_commands(slack_client.rtm_read()):
                            self.high_activity_mode = True
                            self.last_activity_ts = time.time()


                        if time.time() - self.last_activity_ts > HIGH_ACTIVITY_WINDOW_SEC:
                            self.high_activity_mode = False


                        if self.high_activity_mode:
                            time.sleep(HIGH_ACTIVITY_DELAY_S)
                        else:
                            time.sleep(LOW_ACTIVITY_DELAY_S)

                    except Exception as e:
                        print(e)
                        break

            else:
                print("Connection failed. Exception traceback printed above.")
                time.sleep(RECONNECT_DELAY)

    def parse_bot_commands(self, slack_events):
        """
            Parses a list of events coming from the Slack RTM API to find bot commands.
            If a bot command is found, this function returns a tuple of command and channel.
            If its not found, then this function returns None, None.
        """
        # print(slack_events)
        for event in slack_events:
            if event["type"] == "message" and not "subtype" in event:
                if event["channel"].startswith("D"):
                    self.handle_dm_command(event["user"], event["text"], event["channel"])
                    return True
        return False

    def handle_dm_command(self, from_user, text, channel):
        print ("Text: {} from user: {}".format(text, from_user))
        if from_user not in self.conversations:
            self.conversations[from_user] = Conversation(from_user, channel,
                                                         self.user_list[from_user]["real_name"],
                                                         self.user_list[from_user]["profile"]["image_48"])

        # text = re.sub("\<@(.*?)>", lambda x: '@'+self.user_list[re.findall("(?<=\<@).*?(?=>)", x.group(0))[0]]["name"], text)
        print ("Processed text : {}".format(text))
        self.conversations[from_user].incoming_message(text)

class Conversation:
    WaitingForStart, WaitingForCommands = range(2)

    def __init__(self, uid, userid, name, icon_url):
        self.current_status = self.WaitingForStart
        self.uid = uid
        self.userid = userid
        self.name = name
        self.icon_url = icon_url
        self.task_list = taskmanager.get_tasklist(uid)
        self.new_tasks = set()
        self.updated_tasks = set()
        self.last_task_list_ts = None
        self.last_published_list_ts = None

    def incoming_message(self, msg):

        if msg.lower().startswith("start"):
            self.send_response("_Lets begin!_ :punch:\n")
            self.show_help()
            self.show_task_list()
        elif msg.lower().startswith("show"):
            self.show_task_list()
        elif msg.lower().startswith("done"):
            self.check_and_mark_status(Status.DONE, msg, "Great job getting those done! :clap:\n")
            self.show_task_list()
        elif msg.lower().startswith("wip"):
            self.check_and_mark_status(Status.WIP, msg, "Its okay we'll get those tomorrow! :punch:\n")
            self.show_task_list()
        elif msg.lower().startswith("cancelled"):
            self.check_and_mark_status(Status.CANCELLED, msg, "They weren't worth it anyways.. \n")
            self.show_task_list()
        elif msg.lower().startswith("delete"):
            self.check_and_mark_status(Status.DELETED, msg, "Deleted \n")
            self.show_task_list()
        elif msg.lower().startswith("todo"):
            self.check_and_mark_status(Status.NEW, msg, "Back to Todo\n")
            self.show_task_list()
        elif msg.lower().startswith("publish"):
            self.publish()
            self.current_status = self.WaitingForStart
        elif msg.lower().startswith("preview"):
            self.preview()
            self.render_task_list(presentation=True)

        elif msg.startswith("-"):
            lines = msg.strip().split("\n")
            for line in lines:
                new_msg = re.findall("^-(.*)", line)[0].strip()
                if new_msg:
                    new_task = self.add_task(new_msg);
                    self.new_tasks.add(new_task)
                    self.updated_tasks.add(new_task)

            self.show_task_list()
        else:
            self.show_help(error=True)


    def check_and_mark_status(self, status, msg, success_msg):
        task_ids = self.get_task_ids(msg)
        print (task_ids)
        if task_ids:
            if status == Status.DELETED:
                self.delete_tasks(task_ids)
                self.updated_tasks.difference_update(task_ids)
            else:
                self.mark_status(status, task_ids)
                self.updated_tasks.update(task_ids)

            if status != Status.NEW:
                self.new_tasks.difference_update(task_ids)

            self.send_response(success_msg)
        else:
            self.show_help(error=True)

    def show_help(self, error=False):
        if error:
            self.send_response("I don't understand what you mean :confused:")

        self.send_response("_You can tell me what to mark as `done`, `wip` or `cancelled` by saying `done 1,3,6` for example. "
                           "To delete a task use `delete` and to mark it back as todo use `todo`. To simply show the list type `show`. "
                           "To start a new task simply enter a dash and the task. e.g. `- new task for today`_")
        self.send_response("_The task list below will be automagically updated. When done enter `publish` to send it to #"+kPublishToChannel+"_")
        self.send_response("_Standupbot remembers your `todo` and `wip` tasks from yesterday, and will automatically remove your done and cancelled tasks after you publish them. No more remembering and repeating yourself! :sweat_smile:_")

    def publish(self):
        self.send_response("Publishing to " +kPublishToChannel)
        self.last_published_list_ts = self.send_response(self.render_task_list(presentation=True), kPublishToChannel, postAsUser=True)["ts"]
        self.task_list.prune()
        self.updated_tasks.clear()
        self.new_tasks = set()
        self.main_task_list_ts = None
        self.send_response("New task list is now: ")
        self.show_task_list()

    def update_last_published(self):
        self.send_response("Updating last published list on "+kPublishToChannel)
        # WIP needs work!

    def get_task_ids(self, msg):
        only_ids = re.findall("^\w+\s(.*)", msg)[0].strip()
        if only_ids:
            return [int(x.strip()) for x in only_ids.split(",")]
        else:
            return None

    def delete_tasks(self, task_ids):
        if task_ids:
            self.task_list.delete_task(task_ids)

    def mark_status(self, status, task_ids):
        if task_ids:
            for task_id in task_ids:
                if task_id:
                    self.task_list.change_status(task_id, status)

    def add_task(self,msg):
        return self.task_list.add_task(msg)


    def show_task_list(self, update=False):
        if not update:
            self.delete_last_response()

        response = self.send_response(self.render_task_list(), update=update)
        self.last_task_list_ts = response["ts"]
        return response

    def render_task_list(self, presentation=False):
        if not self.task_list.tasks:
            return "_You have no tasks yet, what do you say you do around here?_ :thinking_face: Enter a task by simply typing something like `-new task for today` and this message will get updated.\n"

        past_msg = ""
        new_msg = ""

        for task_id, desc in self.task_list.tasks.items():

            curr_msg = self.render(task_id, desc[1], desc[2], presentation=presentation) + "\n"
            if task_id in self.new_tasks:
                if presentation:
                    new_msg += curr_msg
                else:
                    new_msg += curr_msg
            else:
                past_msg += curr_msg

        msg = ""
        if presentation:
            if past_msg:
                msg += "*Previously*\n"
                msg += past_msg

            if new_msg:
                msg += "\n"
                msg += "*New Tasks*\n"
                msg += new_msg

        else:
            msg = past_msg + new_msg

        msg += "\n"

        return msg

    def send_response(self, msg, channel=None, update=False, postAsUser=False, ts=None):
        if not channel:
            channel = self.userid

        if not ts:
            ts = self.last_task_list_ts

        action = "chat.update" if update else "chat.postMessage"
        username = self.name + " (via @standupbot)" if postAsUser else "Standup"
        iconurl = self.icon_url if postAsUser else None
        return slack_client.api_call(
            action,
            channel=channel,
            username=username,
            icon_url=iconurl,
            ts=ts,
            text=msg
        )

    def delete_last_response(self, ts=None, channel=None):
        if not channel:
            channel = self.userid

        if not ts:
            ts = self.last_task_list_ts

        return slack_client.api_call(
            "chat.delete",
            channel=channel,
            ts=ts
        )

    def get_emoji_for_status(self, status):
        if status == Status.NEW:
            return ":su-todo:"
        elif status == Status.WIP:
            return ":su-wip:"
        elif status == Status.DONE:
            return ":su-done:"
        elif status == Status.CANCELLED:
            return ":su-blocked:"

    def render(self, taskid, desc, status, presentation=False):
        if presentation:
            msg = "{} {}".format(self.get_emoji_for_status(status), desc)
        else:
            msg = "{}:{} {}".format(taskid, self.get_emoji_for_status(status), desc)

        # if taskid in self.updated_tasks:
        #     msg = "_" + msg + "_"
        return msg

if __name__ == "__main__":
    Bot().mainloop()
