from mitmproxy import ctx, http
from bs4 import BeautifulSoup
from db import LogDatabase
from checkedDomains import CheckedDomains
from iptableSetup import IptablesHandler
from windowsFirewallHandler import WindowsFirewallHandler
import subprocess, json, requests, atexit, sys, time, os, urllib, tldextract, hashlib

text_clf = None
options = {
    "block-ads" : True,
    "block-malicious" : True,
    "isBlacklist" : True,
    "block-child-unsafe-level": 80,
    "block-suspicious-level": 80
}

blockedDomains = {
    "ad": set(),
    "malicious" : set(),
    "user" : set(),
    "exclude" : set()
}

categories = {
    '101': 'Negative: Malware or viruses',
    '102': 'Negative: Poor customer experience',
    '103': 'Negative: Phishing',
    '104': 'Negative: Scam',
    '105': 'Negative: Potentially illegal',
    '201': 'Questionable: Misleading claims or unethical',
    '202': 'Questionable: Privacy risks',
    '203': 'Questionable: Suspicious',
    '204': 'Questionable: Hate, discrimination',
    '205': 'Questionable: Spam',
    '206': 'Questionable: Potentially unwanted programs',
    '207': 'Questionable: Ads / pop-ups',
    '301': 'Neutral: Online tracking',
    '302': 'Neutral: Alternative or controversial medicine',
    '303': 'Neutral: Opinions, religion, politics ',
    '304': 'Neutral: Other ',
    '401': 'Child safety: Adult content',
    '402': 'Child safety: Incindental nudity',
    '403': 'Child safety: Gruesome or shocking',
    '404': 'Child safety: Site for kids',
    '501': 'Positive: Good site'
}

with open("../../proxy.config") as proxyConfigFile:
    config = json.load(proxyConfigFile)

#spin up mongo server
if not sys.platform.startswith('linux'):
    mongoServerP = subprocess.Popen([config["winMongoPath"], "--dbpath", "../data/mongodb"],
        creationflags=subprocess.CREATE_NEW_CONSOLE)
    #close mongo server on exit
    import atexit
    atexit.register(mongoServerP.terminate)


checkedDomains = {}
apiKeys = None
log = None
extract = tldextract.TLDExtract(suffix_list_urls=None)
with open("../data/apiKeys.json", "r") as f:
    apiKeys = json.loads(f.read())

def addDomainsF(fileName, category):
    with open(fileName, "r") as f:
        domains = f.readlines()
        for d in domains:
            d2 = d.replace("\n", "")
            blockedDomains[category].add(d2)

def addDomainGroup(groupNames):
    with open('../data/domain-groups.json', 'r') as dg:
        domainGroups = json.loads(dg.read())

        for dgName in groupNames:
            for dgDomain in domainGroups[dgName]:
                blockedDomains["user"].add(dgDomain)

test_rules = False
def load(l):
    #build hash table of domains to block
    addDomainsF("../data/ad-domains-full.txt", "ad")
    addDomainsF("../data/malicious-domains.txt", "malicious")

    #load user defined domains
    if test_rules:
        with open('../data/testrules.json', 'r') as rulesFile:
            rules = json.loads(rulesFile.read())
    else:
        #retrive policy
        time.sleep(3) #wait a while for node client to load rules
        policyRequest = requests.get('http://localhost:3000/rules.json')
        policyJson = policyRequest.json()
        rules = policyJson

    print(json.dumps(rules, indent=4))

    #webfilter setup
    r = rules["webfilter"]
    #set options
    #mode
    if "mode" not in r or r["mode"] == "blacklist":
        options["isBlacklist"] = True
    else:
        options["isBlacklist"] = False
    #blocking of ads and malicious sites
    if "blockAds" not in r or r["blockAds"]:
        options["block-ads"] = True
    else:
        options["block-ads"] = False
    if "blockMalicious" not in r or r["blockMalicious"]:
        options["block-malicious"] = True
    else:
        options["block-malicious"] = False
    if "childSafety" not in rules or rules["childSafety"]:
        options["block-child-unsafe"] = True
    else:
        options["block-child-unsafe"] = False
    if "virusScan" not in rules or rules["virusScan"]:
        options["virus-scan"] = True
    else:
        options["virus-scan"] = False

    print(json.dumps(options, indent=4))

    #get domain groups
    #reformat data again
    domainGroups = [];
    if "fakeNews" in r and r["fakeNews"]: domainGroups.append("fakeNews")
    if "gambling" in r and r["gambling"]: domainGroups.append("gambling")
    if "socialMedia" in r and r["socialMedia"]: domainGroups.append("socialMedia")
    if "pornography" in r and r["pornography"]: domainGroups.append("pornography")
    addDomainGroup(domainGroups)
    #add user-defined domains
    if "blacklist" in r:
        for domain in r["blacklist"]:
            full = extract(domain)
            d = '.'.join([full.domain, full.suffix])
            blockedDomains["user"].add(d)
    if "whitelist" in r:
        for domain2 in r["whitelist"]:
            full2 = extract(domain2)
            d2 = '.'.join([full2.domain, full2.suffix])
            blockedDomains["exclude"].add(d2)

def request(flow):
    fullDomain = extract(flow.request.pretty_host)
    d = '.'.join([fullDomain.domain, fullDomain.suffix])
    #d = flow.request.pretty_host
    if (d == 'api.mywot.com' or d == 'safebrowsing.googleapis.com'):
        return

    #get ip of requester
    print(flow.client_conn.ip_address)
    ip = flow.client_conn.ip_address[0][7:]
    LogDatabase.request(ip, d)

    #handle exclusions
    if d in blockedDomains["exclude"]:
        return

    apiSkip = False
    #check domain if not checked already, and log it into CheckedDomains
    if CheckedDomains.search(d) is None:
        #ignore ads
        if options["block-ads"]:
            if d in blockedDomains["ad"]:
                CheckedDomains.add(d, False, "Blocked by policy (listed advertisement)", True)
                apiSkip = True
        #block malicious websites
        if options["block-malicious"]:
            if d in blockedDomains["malicious"]:
                CheckedDomains.add(d, False, "Blocked by policy (listed malicious)", False)
                apiSkip = True
        #block user defined sites
        #blacklist
        if options["isBlacklist"]:
            if d in blockedDomains["user"]:
                print("USER DEFINED BLOCKED DOMAIN--------------" +d)
                apiSkip = True
                LogDatabase.blockedDomain(ip, d)
                CheckedDomains.add(d, False, "Blocked by policy (user blacklist)", False)
        else: #whitelist
            if flow.request.pretty_host not in blockedDomains["user"]:
                apiSkip = True
                LogDatabase.blockedDomain(ip, d)
                CheckedDomains.add(d, False, "Blocked by policy (user whitelist)", False)

        if not apiSkip:
            #lookup stuff in the apis
            wotResults = webOfTrustLookup(d)
            gResults = googleSafeBrowsingLookup(d)
            #getting api results
            results = {}
            #google safe browsing
            if gResults != 0 and gResults != 1:
                if len(gResults) > 0:
                    results["threatType"] = gResults
                    CheckedDomains.add(d, False, "Dangerous site", False)
                    LogDatabase.securityEvent(ip, d, "suspiciousDomain")
            else:
                () #TODO: log failure to another database

            #web of trust
            wotR = wotResults["reputation"]
            if wotR["trustworthiness"] is not None:
                results["trustworthiness"] = wotR["trustworthiness"][0]
                results["trustworthiness-confidence"] =wotR["trustworthiness"][1]
            if wotR["childSafety"] is not None:
                results["childSafety"] = wotR["childSafety"][0]
                results["childSafety-confidence"] = wotR["childSafety"][1]
            results["categories"] = []
            results["categoryTypes"] = []
            for value in wotResults["categories"]:
                results["categories"].append(value)
                results["categoryTypes"].append(value[0][0])
            print(results)


            #check api call results
            if options["block-child-unsafe"]:
                if "childSafety" in results:
                    if results["childSafety"] < options["block-child-unsafe-level"] and \
                     results["childSafety-confidence"] >= 5:
                        CheckedDomains.add(d, False, "Blocked by policy (child safety)", False)
                        LogDatabase.securityEvent(ip, d, "childUnsafe")
            if options["block-malicious"]:
                if "trustworthiness" in results:
                    if results["trustworthiness"] < options["block-suspicious-level"] and \
                     results["trustworthiness-confidence"] > 30:
                        CheckedDomains.add(d, False, "Blocked by policy (suspicious)", False)
                        LogDatabase.securityEvent(ip, d, "suspiciousDomain")
            for cat in results["categories"]:
                if cat[0][0] == "1" and int(cat[1]) > 90:
                    CheckedDomains.add(d, False, "Blocked by policy (" + category[cat[0]])
                    LogDatabase.securityEvent(ip, d, str(cat[0]))
                if cat[0][0] == "2" and int(cat[1]) > 70:
                    CheckedDomains.add(d, False, "Blocked by policy (" + catefory[cat[0]])
                    LogDatabase.securityEvent(ip, d, str(cat[0]))
                if cat[0][0] == "3" and int(cat[1]) > 40:
                    () #TODO: Add filters for certain topics


        #passed all the above checks -> safe domain
        if CheckedDomains.search(d) is None:
            CheckedDomains.add(d, True, None, False)

    #lookup domain and decide course of action
    sc = CheckedDomains.search(d)
    if not sc["isSafe"]: #previously logged as unsafe
        if sc["kill"]:
            flow.response = http.HTTPResponse.make(
                200, ""
            )
        else:
            #list all reasons the domain is bad
            r = ""
            for reason in sc["reason"]:
                r += reason
            b = {"reason" : r}
            flow.request.url = "http://" + config["nodeClientAddress"] + ":" + config["nodeClientPort"] + "/blocked?" + urllib.parse.urlencode(b)
    else:
        print("SAFE DOMAIN-----------"+d)


def response(flow):
    #remove subdomain
    fullDomain = extract(flow.request.pretty_host)
    d = '.'.join([fullDomain.domain, fullDomain.suffix])

    ip = flow.client_conn.ip_address[0][7:]
    if flow.response.headers.get("content-type", "").startswith("image"):
        () #TODO:: check images (may abondon for performance reasons)
    elif flow.response.headers.get("content-type", "").startswith("video"): #video
        () #Do nothing
    elif flow.response.headers.get("content-type", "").startswith("application/octet-stream") or \
     flow.response.headers.get("content-disposition", "").startswith("attachment") or \
     "x-msdownload" in flow.response.headers.get("content-type", ""): #downloaded files
        if options["virus-scan"]:
            #scan url for viruses
            params = {'apikey': apiKeys["virusTotal"], 'resource': flow.request.url}
            response = requests.post('https://www.virustotal.com/vtapi/v2/url/report',
              params=params)
            if response.status_code == 200:
                vtJson = response.json()
                if "positives" in vtJson:
                    if vtJson["positives"] <= 0:
                        #log downloaded file event
                        LogDatabase.downloadFile(ip, d, flow.request.url, True)
                    else:
                        #not safe, stop download
                        LogDatabase.downloadFile(ip, d, flow.request.url, False)
                        flow.response = http.HTTPResponse.make(
                            418, "Malicious file detected"
                        )
                else:#url not recognized, trying out file hash
                    hasher = hashlib.sha256()
                    hasher.update(flow.response.content)
                    hash = hasher.hexdigest()
                    params2 = {'apikey': apiKeys["virusTotal"], 'resource': hash}
                    headers2 = {
                        "Accept-Encoding": "gzip, deflate",
                    }
                    response2 = requests.get('https://www.virustotal.com/vtapi/v2/file/report',
                      params=params2, headers=headers2)
                    json_response = response2.json()
                    if "positives" in json_response:
                        if json_response["positives"] <= 0: #safe
                            print("SAFE DOWNLOAD - VIA HASH")
                            LogDatabase.downloadFile(ip, d, flow.request.url, True)
                        else:#unasfe
                            print("UNSAFE DOWNLOAD - HASH CHECK")
                            LogDatabase.downloadFile(ip, d, flow.request.url, False)
                            flow.response = http.HTTPResponse.make(
                                418, "Malicious file detected"
                            )
                    else: #still no record
                        () #Do nothing. I can't think of a good way to use their api
            else: #failed connection, possibly over the api limit
                () #do nothing
    else:
        #html text
        () #do nothing

#Query Web of Trust for site reputation and category
def webOfTrustLookup(domain):
    if domain in checkedDomains:
        return True
    else:
        results = {
            'reputation' : {
                'trustworthiness' : None,
                'childSafety' : None
            },
            'categories' : []
        }
        try:
            target = 'http://' + domain + '/'
            parameters = {'hosts': domain + "/", 'key': apiKeys["webOfTrust"]}
            reply = requests.get(
                "http://api.mywot.com/0.4/public_link_json2",
                params=parameters,
                headers={'user-agent': 'Mozilla/5.0'})
            reply_dict = json.loads(reply.text)
            if reply.status_code == 200:
                for key, value in reply_dict[domain].items():
                    if key == "1":
                        ()  # Deprecated
                    elif key == "2":
                        ()  # Deprecated
                    elif key == "0":
                        if value is None:
                            results["reputation"]["trustworthiness"] = (0, 100)
                        results["reputation"]["trustworthiness"] = value
                    elif key == "4":
                        if value is None:
                            results["reputation"]["trustworthiness"] = (0, 100)
                        results["reputation"]["childSafety"] = value
                    elif key == "categories":
                        for categoryId, confidence in value.items():
                            results["categories"].append((categoryId, confidence))
                    elif key == "blacklists":
                        continue #lazy to handle this
                    else:
                        continue
                return results
            if reply.status_code != 200:
                return 2 #Server return unusual status code
        except KeyError:
            return 0 #Web of Trust API key does not work

def googleSafeBrowsingLookup(domain):
    result = []
    try:
        headers = {'content-type': 'application/json'}
        payload = {
            'client' : {
                'clientId' : 'OCCS_Project',
                'clientVersion': '1.0'
            },
            'threatInfo' : {
                'threatTypes' : ["MALWARE", "SOCIAL_ENGINEERING"],
                'platformTypes' : ["ANY_PLATFORM"],
                'threatEntryTypes' : ["URL"],
                'threatEntries' : [{"url": domain}]
            }
        }
        reply = requests.post('https://safebrowsing.googleapis.com/v4/threatMatches:find?key=' + apiKeys['googleSafeBrowsing'],
            headers=headers, json=payload)

        if reply.status_code == 200:
            j = reply.json()
            if j:
                for match in reply.json()["matches"]:
                    result.append(match['threatType'])
            return result
        else:
            return 1 #bad request
    except KeyError:
        return 0 #Google API key does not work
