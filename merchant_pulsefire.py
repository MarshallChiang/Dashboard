import plotly.offline as py
import plotly.graph_objs as go 
import colorlover as cl
from plotly.offline import plot 
from plotly import subplots
from multiprocessing.dummy import Pool
import pandas as pd 
import numpy as np
import requests
import datetime
import boto3
import json
import time
import re
import os

s3 = boto3.client('s3')
NetworkToken = os.environ["NetworkToken"]
api_url = os.environ["api_url"]
data_source = {
    "public_variables" : lambda i, s, e, p=1 : sum([
        [
            {"NetworkToken" : NetworkToken},
            {"filters[Stat.date][values][]" : s},
            {"filters[Stat.date][values][]" : e},
            {"page": p},
            {"limit" : 10000}
        ],[
            {"filters[Stat.offer_id][values][]" : ii} for ii in i
        ]
        ], []),
    "sources" : [ # each call for each dict merged with public variable.
        {
            "variables" : [
                {"Target" : "Report"},
                {"Method" : "getStats"},
                {"fields[]" : "Stat.date"},
                {"fields[]" : "Stat.hour"},
                {"fields[]" : "Stat.clicks"},
                {"fields[]" : "Stat.offer_id"},
                {"filters[Stat.date][conditional]" : "BETWEEN"},
                {"filters[Stat.offer_id][conditional]" : "EQUAL_TO"}
            ],
            "index" : lambda x : x["date"] + " " + x["hour"] + ":00:00"
        },
        {
            "variables" : [
                {"Target" : "Report"},
                {"Method" : "getConversions"},
                {"fields[]" : "Stat.datetime"},
                {"fields[]" : "Stat.payout"},
                {"fields[]" : "Stat.offer_id"},
                {"fields[]" : "Stat.sale_amount"},
                {"fields[]" : "Stat.advertiser_info"},
                {"fields[]" : "ConversionsMobile.adv_sub2"},
                {"fields[]" : "ConversionsMobile.adv_sub3"},
                {"fields[]" : "ConversionsMobile.adv_sub4"},
                {"fields[]" : "ConversionsMobile.adv_sub5"},
                {"fields[]" : "Offer.name"},
                {"filters[Stat.date][conditional]" : "BETWEEN"},
                {"filters[Stat.offer_id][conditional]" : "EQUAL_TO"},
            ],
            "index" : lambda x : x["datetime"]
        }
    ]
}
default_metrics = {
    "advertiser_info" : "nunique",
    "adv_sub2" : "count",
    "adv_sub3" : "count",
    "adv_sub4" : "count",
    "adv_sub5" : "count",
    "payout" : "sum",
    "sale_amount" : "sum",
    "clicks" : "sum"
}
columns_name = {
    "t_index" : "datetime",
    "payout" : "commission",
    "clicks" : "clicks",
    "sale_amount" : "gmv",
    "advertiser_info" : "orders",
    "adv_sub2" : "category",
    "adv_sub3" : "sku",
    "adv_sub5" : "product"
}
#get rid of colors too light.
colors = [r for r in sum([cl.scales['11']['div'][c]  for c in ['RdYlBu', 'PuOr', 'BrBG']], []) if sum(tuple(map(int, re.findall(r'[0-9]+', r)))) <= 600] 

def url_concator(self, u, ps) :
    u += "?"
    params_list = list()
    for p in ps :
        params_list += [str(k) + "=" + str(v) for k, v in p.items()]
    u += "&".join(params_list)
    return u

class MainLayer(object) :
    
    def __init__ (self, offer_ls) :

        self.offer_ls = offer_ls

    def get_data(self, start=None, end=None) :

        df = pd.DataFrame()
        for s in data_source["sources"] : 
            sub_df = pd.DataFrame()
            params = s["variables"] + data_source["public_variables"](self.offer_ls, start, end)
            response = api_req(api_url, params).json()
            pageCount = response["response"]["data"]["pageCount"]
            if not pageCount : break
            pool = Pool(int(pageCount) if int(pageCount) < 1 else 1)
            output = []
            for i in range(1, pageCount + 1) : 
                p = s["variables"] + data_source["public_variables"](self.offer_ls, start, end, p=i)
                output.append(pool.apply_async(api_req, (api_url, p), {"error_handle":{"value" : 1, "keys":["response", "status"]}}))
            pool.close()
            result = [o.get() for o in output]
            for r in result :
                data = r.json()
                append_ls = list()
                for d in data["response"]["data"]["data"] : 
                    append_dict = dict()
                    for k, v in d.items() :
                        for ek, ev in v.items() :
                            append_dict[ek] = ev if ev != "" else np.nan
                    append_ls.append(append_dict)
                sub_df = sub_df.append(pd.DataFrame(append_ls), ignore_index=True)
            sub_df["t_index"] = sub_df.apply(s["index"], axis=1)
            df = df.append(sub_df, ignore_index=True, sort=True)
            
        df.index = pd.to_datetime(df["t_index"]).rename(index={"t_index":"t"})
        self.df = df
        self.start = start
        self.end = end

    def layout_default(self, offer_id, column, metrics=default_metrics, interval="1H") :
        
        default_layout = self.df.loc[self.df['offer_id'].isin(offer_id)] 
        name = default_layout['name'].value_counts().index[0] if len(default_layout) != 0 else None
        for k, v in metrics.items() :
            if v in ("mean", "sum", "max", "min", "std") :
                default_layout[k] = default_layout[k].apply(pd.to_numeric)
        default_layout = default_layout.dropna(axis=1, how="all")
        metrics = { k : v for k , v in metrics.items() if k in default_layout.columns}
        default_layout = default_layout.groupby(pd.Grouper(freq=interval)).agg(metrics)
        return {"offer_id" : offer_id, "fig_type":"default", "group_key" : "default", "benchmark" : column, "groupObject" : default_layout[column], "fig_name" : name}
    
    def layout_sub_attribution(self, offer_id, benchmark, group_by, offset, interval="1H") :
        sub_layout = self.df.loc[self.df['offer_id'] == offer_id]
        sub_layout.fillna("", inplace=True)
        name = sub_layout['name'].value_counts().index[0] if len(sub_layout) != 0 else None
        if default_metrics[benchmark] in ("mean", "sum", "max", "min", "std") :
            sub_layout[benchmark] = sub_layout[benchmark].apply(pd.to_numeric)
        sub_layout = sub_layout.groupby([pd.Grouper(freq=interval), group_by]).agg({benchmark : default_metrics[benchmark]})[benchmark].groupby(level=0).nlargest(offset).to_frame()
        sub_layout.reset_index(level=group_by, inplace=True)
        return {"offer_id" : offer_id, "fig_type":"attribution", "group_key" : group_by, "benchmark" : benchmark, "groupObject" : sub_layout, "fig_name" : name}


def get_figure(offers, lookup, start, end, interval="1H", offset=15) : 

    '''
    if isinstance(lookup, list) >> MainLayer object will be using layout_default and reshape the dataframe columns for layout.
    >> lookup = [C1, C2, C3 ...]
    if isinstance(lookup, dict) >> MainLayer object will be using layout_sub_attribution to plot attribution by dict's key and values
    >> lookup = {"benchmark" : C1}
    '''
    cols = list(lookup.values())[0] if isinstance(lookup, dict) else lookup
    fig = subplots.make_subplots(rows=len(offers), cols=len(cols), subplot_titles=tuple(cols)*len(offers))
    parent_df = pd.DataFrame()
    #initialize the MainLayer object and get data.
    m = MainLayer(offers)
    m.get_data(start=start, end=end)
    for oi, o in enumerate(offers) : 
        odf = pd.DataFrame()
        if isinstance(lookup, list) :
            for ci, c in enumerate(lookup) :
                f_data = m.layout_default([o], lookup, interval=interval)
                fig.add_trace(
                    go.Scatter(
                        x = f_data["groupObject"].index,
                        y = f_data["groupObject"][c],
                        marker = dict(
                            color="%s"%colors[(len(colors)//len(offers))*oi] 
                        ),        
                    ),
                    row = oi + 1,
                    col = ci + 1
                )
                fig['layout']['annotations'][ci + len(lookup)*(oi)]['text'] = f_data['fig_name'] + ' ' + columns_name[c]
                sub_df = f_data["groupObject"].reset_index(level=0)
                sub_df.insert(loc=1, column="name", value=f_data["fig_name"])
                sub_df.rename(columns=columns_name, inplace=True)
                odf = sub_df if odf.empty else pd.merge(odf, sub_df, on=['datetime', 'name'], how='left')

        elif isinstance(lookup, dict) :
            k, v = list(lookup.keys())[0], list(lookup.values())[0]
            for vi, l in enumerate(v) :
                f_data = m.layout_sub_attribution(o, l, k, offset=offset, interval=interval)
                for n in range(0, offset) : 
                    ndf = f_data["groupObject"].groupby(level=0).nth(n)
                    fig.add_trace(
                        go.Bar(
                            x = ndf.index,
                            y = ndf[f_data["benchmark"]],
                            text =  ndf[f_data["group_key"]],
                            hovertemplate = "<b>%s</b><br><br>"%f_data["fig_name"] + 
                            "<i>Product</i> : %{text}<br>"
                            "<i>Time</i> : %{x}<br>"
                            "<i>Summary</i> : %{y}",
                            textposition = "auto",
                            marker = dict(
                                color="%s"%colors[(len(colors)//offset)*n]
                            )
                        ),
                        row = oi + 1,
                        col = vi + 1
                    )
                    fig.update_layout(barmode="overlay", showlegend=False, title='%s Performance of %s'%(interval_text(interval), ' - '.join([start[:10], end[:10]]) if start[:10] != end[:10] else start[:10]))
                fig['layout']['annotations'][vi + len(v)*(oi)]['text'] = f_data['fig_name'] + ' ' + columns_name[l]
                sub_df = f_data["groupObject"].reset_index(level=0)
                sub_df.insert(loc=1, column="name", value=f_data["fig_name"])
                sub_df.rename(columns=columns_name, inplace=True)
                print(sub_df)
                # odf = sub_df if odf.empty else pd.merge(odf, sub_df, on=['datetime', 'name', columns_name[k]], how='left')
                odf = odf.append(sub_df, sort=False)
        else :
            raise ValueError("[Error] Invalid Lookup Format As %s. Should Be Either Dict or List."%type(lookup))

        parent_df = parent_df.append(odf, sort=False)

    
    pid = '%s_%s'%(sum(int(s) for s in m.offer_ls), datetime.datetime.now().strftime('%s'))
    p = plot(fig, filename='/tmp/%s.html'%pid, auto_open=False)
    dl_df = parent_df.astype(str)
    return {
        "pid" : pid,
        "p_type" : "default" if isinstance(lookup, list) else "attr",
        "p_data_list" : [dl_df.columns.values.tolist()] + dl_df.values.tolist()
    }

def interval_text(t) : 
    text = ''
    if 'H' in t :
        text = 'Hourly'
    elif 'D' in t :
        text =  'Daily'
    elif 'W' in t :
        text = 'Weekly'
    elif 'M' in t :
        text = 'Monthly'
    return text

def api_req(url, kvp, error_handle=None) :
    url += "?"
    params_list = list()
    for kv in kvp :
        params_list += [str(k) + "=" + str(v) for k, v in kv.items()]
    url += "&".join(params_list)
    print(url)
    r = requests.get(url)
    time.sleep(0.2)
    if error_handle :
        ev, eks = error_handle["value"], error_handle["keys"]
        parent = r.json()
        for i, ek in enumerate(eks) :
            parent = parent[ek]
            if i + 1 == len(eks) and parent != ev :
                r = api_req(url, kvp, error_handle = error_handle)
    return r 

def lambda_handler(even, context) :
    data = even
    poll_token = even["poll_token"]
    # try :
    output = get_figure(data["offers"], data["lookup"], data["start"], data["end"], interval=data["interval"])
    with open("/tmp/%s.html"%output["pid"], "rb") as data :
        s3.put_object(Bucket=os.environ["bucket"], Key=poll_token + ".html", Body=data, ACL='public-read', ContentType='text/html')
        s3.put_object(Bucket=os.environ["bucket"], Key=poll_token + ".json", Body=json.dumps(output["p_data_list"]).encode("utf-8-sig"), ACL='public-read')
        return json.dumps(HttpsResponse(200, True, poll_token))
    # except Exception as e :
    #     return HttpsResponse(500, False, str(e))

def printProgressBar (iteration, total, prefix = "", suffix = "", decimals = 1, length = 100, fill = "█", printEnd = "\r"):
    """
    Call in a loop to create terminal progress bar
    @params:
        iteration   - Required  : current iteration (Int)
        total       - Required  : total iterations (Int)
        prefix      - Optional  : prefix string (Str)
        suffix      - Optional  : suffix string (Str)
        decimals    - Optional  : positive number of decimals in percent complete (Int)
        length      - Optional  : character length of bar (Int)
        fill        - Optional  : bar fill character (Str)
        printEnd    - Optional  : end character (e.g. "\r", "\r\n") (Str)
    """
    percent = ("{0:." + str(decimals) + "f}").format(100 * (iteration / float(total)))
    filledLength = int(length * iteration // total)
    bar = fill * filledLength + "-" * (length - filledLength)
    print("\r%s |%s| %s%% %s" % (prefix, bar, percent, suffix), end = printEnd)
    # Print New Line on Complete
    if iteration == total: 
        print()

def HttpsResponse(statusCode, Bol, Message) :
    
    return {
        'statusCode' : statusCode,
        'body' : json.dumps({
            'Success' : Bol,
            'Message' : Message
        })
    }
