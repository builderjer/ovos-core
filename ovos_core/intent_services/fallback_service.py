# Copyright 2020 Mycroft AI Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
"""Intent service for Mycroft's fallback system."""
import operator
from collections import namedtuple

import time
from ovos_config import Configuration

import ovos_core.intent_services
from ovos_utils import flatten_list
from ovos_utils.log import LOG
from ovos_workshop.skills.fallback import FallbackMode

FallbackRange = namedtuple('FallbackRange', ['start', 'stop'])


class FallbackService:
    """Intent Service handling fallback skills."""

    def __init__(self, bus):
        self.bus = bus
        self.fallback_config = Configuration()["skills"].get("fallbacks", {})
        self.registered_fallbacks = {}  # skill_id: priority
        self.bus.on("ovos.skills.fallback.register", self.handle_register_fallback)
        self.bus.on("ovos.skills.fallback.deregister", self.handle_deregister_fallback)

    def handle_register_fallback(self, message):
        skill_id = message.data.get("skill_id")
        priority = message.data.get("priority") or 101

        # check if .conf is overriding the priority for this skill
        priority_overrides = self.fallback_config.get("fallback_priorities", {})
        if skill_id in priority_overrides:
            new_priority = priority_overrides.get(skill_id)
            LOG.info(f"forcing {skill_id} fallback priority from {priority} to {new_priority}")
            self.registered_fallbacks[skill_id] = new_priority
        else:
            self.registered_fallbacks[skill_id] = priority

    def handle_deregister_fallback(self, message):
        skill_id = message.data.get("skill_id")
        if skill_id in self.registered_fallbacks:
            self.registered_fallbacks.pop(skill_id)

    def _fallback_allowed(self, skill_id):
        """Checks if a skill_id is allowed to fallback

        - is the skill blacklisted from fallback
        - is fallback configured to only allow specific skills

        Args:
            skill_id (str): identifier of skill that wants to fallback.

        Returns:
            permitted (bool): True if skill can fallback
        """
        opmode = self.fallback_config.get("fallback_mode", FallbackMode.ACCEPT_ALL)
        if opmode == FallbackMode.BLACKLIST and skill_id in \
                self.fallback_config.get("fallback_blacklist", []):
            return False
        elif opmode == FallbackMode.WHITELIST and skill_id not in \
                self.fallback_config.get("fallback_whitelist", []):
            return False
        return True

    def _collect_fallback_skills(self, message, fb_range=FallbackRange(0, 100)):
        """use the messagebus api to determine which skills have registered fallback handlers
        This includes all skills and external applications"""
        skill_ids = []  # skill_ids that already answered to ping
        fallback_skills = []  # skill_ids that want to handle fallback

        # filter skills outside the fallback_range
        in_range = [s for s, p in self.registered_fallbacks.items()
                    if fb_range.start < p <= fb_range.stop]
        skill_ids += [s for s in self.registered_fallbacks if s not in in_range]

        def handle_ack(msg):
            skill_id = msg.data["skill_id"]
            if msg.data.get("can_handle", True):
                if skill_id in self.registered_fallbacks:
                    fallback_skills.append(skill_id)
                LOG.info(f"{skill_id} will try to handle fallback")
            else:
                LOG.info(f"{skill_id} will NOT try to handle fallback")
            skill_ids.append(skill_id)

        self.bus.on("ovos.skills.fallback.pong", handle_ack)

        LOG.info("checking for FallbackSkillsV2 candidates")
        # wait for all skills to acknowledge they want to answer fallback queries
        self.bus.emit(message.forward("ovos.skills.fallback.ping",
                                      message.data))
        start = time.time()
        while not all(s in skill_ids for s in self.registered_fallbacks) \
                and time.time() - start <= 0.5:
            time.sleep(0.02)

        self.bus.remove("ovos.skills.fallback.pong", handle_ack)
        return fallback_skills

    def attempt_fallback(self, utterances, skill_id, lang, message):
        """Call skill and ask if they want to process the utterance.

        Args:
            utterances (list of tuples): utterances paired with normalized
                                         versions.
            skill_id: skill to query.
            lang (str): current language
            message (Message): message containing interaction info.

        Returns:
            handled (bool): True if handled otherwise False.
        """
        if self._fallback_allowed(skill_id):
            fb_msg = message.reply(f"ovos.skills.fallback.{skill_id}.request",
                                   {"skill_id": skill_id,
                                    "utterances": utterances,
                                    "utterance": utterances[0],  # backwards compat, we send all transcripts now
                                    "lang": lang})
            result = self.bus.wait_for_response(fb_msg,
                                                f"ovos.skills.fallback.{skill_id}.response")
            if result and 'error' in result.data:
                error_msg = result.data['error']
                LOG.error(f"{skill_id}: {error_msg}")
                return False
            elif result is not None:
                return result.data.get('result', False)
        return False

    def _fallback_range(self, utterances, lang, message, fb_range):
        """Send fallback request for a specified priority range.

        Args:
            utterances (list): List of tuples,
                               utterances and normalized version
            lang (str): Langauge code
            message: Message for session context
            fb_range (FallbackRange): fallback order start and stop.

        Returns:
            IntentMatch or None
        """
        # we call flatten in case someone is sending the old style list of tuples
        utterances = flatten_list(utterances)
        message.data["utterances"] = utterances  # all transcripts
        message.data["lang"] = lang

        # new style bus api
        fallbacks = [(k, v) for k, v in self.registered_fallbacks.items()
                     if k in self._collect_fallback_skills(message, fb_range)]
        sorted_handlers = sorted(fallbacks, key=operator.itemgetter(1))
        for skill_id, prio in sorted_handlers:
            result = self.attempt_fallback(utterances, skill_id, lang, message)
            if result:
                return ovos_core.intent_services.IntentMatch('Fallback', None, {}, None)

        # old style deprecated fallback skill singleton class
        LOG.debug("checking for FallbackSkillsV1")
        msg = message.reply(
            'mycroft.skills.fallback',
            data={'utterance': utterances[0],
                  'lang': lang,
                  'fallback_range': (fb_range.start, fb_range.stop)}
        )
        response = self.bus.wait_for_response(msg, timeout=10)

        if response and response.data['handled']:
            return ovos_core.intent_services.IntentMatch('Fallback', None, {}, None)
        return None

    def high_prio(self, utterances, lang, message):
        """Pre-padatious fallbacks."""
        return self._fallback_range(utterances, lang, message,
                                    FallbackRange(0, 5))

    def medium_prio(self, utterances, lang, message):
        """General fallbacks."""
        return self._fallback_range(utterances, lang, message,
                                    FallbackRange(5, 90))

    def low_prio(self, utterances, lang, message):
        """Low prio fallbacks with general matching such as chat-bot."""
        return self._fallback_range(utterances, lang, message,
                                    FallbackRange(90, 101))
