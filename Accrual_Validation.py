import json
import os
import boto3
import urllib3
import pandas as pd
import datetime
import io
import mysql.connector
import re
from dateutil.parser import parse
import awswrangler as wr
import smtplib  
import email.utils
import boto3
import urllib3
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication

def lambda_handler(event, context):
    #return
    ### function will trigger off an upload of an accural data submission
    s3_client = boto3.client("s3")
    s3_resource = boto3.resource("s3")
    ssm = boto3.client("ssm")
    pd.options.mode.chained_assignment = None  # default='warn'
###################################################################################################################################
##  user defined variables, assign to paramenter store
    cbc_name_list = {"cbc01" : ["Feinstein_CBC01", 41], "cbc02": ["UMN_CBC02", 27], "cbc03": ["ASU_CBC03", 32], "cbc04": ["Mt_Sinai_CBC04",14]}  

    passing_msg = ("File is a valid Zipfile. No errors were found in submission. Files are good to proceed to Data Validation")
    #pass_bucket = "seronet-trigger-submissions-passed"
    #fail_bucket = "seronet-demo-submissions-failed"
    pass_bucket = ssm.get_parameter(Name="pass_bucket", WithDecryption=True).get("Parameter").get("Value")
    fail_bucket = ssm.get_parameter(Name="fail_bucket", WithDecryption=True).get("Parameter").get("Value")
###################################################################################################################################
    #set up success and failure slack channel
    slack_fail = ssm.get_parameter(Name="failure_hook_url", WithDecryption=True).get("Parameter").get("Value")
    slack_pass = ssm.get_parameter(Name="success_hook_url", WithDecryption=True).get("Parameter").get("Value")

    bucket = event["Records"][0]["s3"]["bucket"]["name"]
    file_path = event["Records"][0]["s3"]["object"]["key"]
    # Accrual_Need_To_Validate/cbc01/2023-04-20-12-59-25/submission_007_Prod_data_for_feinstein20230420_VaccinationProject_Batch9_shippingmanifest.zip/File_Validation_Results/Result_Message.txt
    file_path = file_path.replace("+", " ")         #some submissions might have spaces this line corrects the "+" replacement
    
    
    curr_cbc = file_path.split("/")[1]
    sub_name = file_path.split("/")[3][15:]
    site_name = cbc_name_list[curr_cbc][0]
    
    print(f"Submission was triggered off the file: {file_path}")
    
    template_path = file_path.split("/")[0] + "/Accrual_Templates"
    template_files = s3_client.list_objects_v2(Bucket=bucket, Prefix=template_path)["Contents"]
    
    template_file_key = [i["Key"] for i in template_files]
    template_files = [i.split("/")[-1] for i in template_file_key ]
    
    col_names = []
    for curr_key in template_file_key:
        if ".xlsx" in curr_key:
            df = wr.s3.read_excel(path=f's3://{bucket}/{curr_key}')
            col_names = col_names + [list(df.columns)]
        
    part_cols = col_names[0]
    vacc_cols = col_names[1]
    visit_cols = col_names[2]
###################################################################################################################################
## Validation step 1: ensure file passed file-validation
    resp = s3_client.get_object(Bucket=bucket, Key=file_path)
    result_message = resp['Body'].read().decode('utf-8')
    
    if result_message != passing_msg:
        email_msg = "1) Analysis of the Zip File: Failed\n " + result_message
        email_msg = email_msg + "\n\n Please correct and resubmit the accrual submission"
        send_error_email(ssm, sub_name, [], [], email_msg, slack_pass, slack_fail, bucket, file_path, 
                         pass_bucket, fail_bucket, s3_client, s3_resource, site_name)     # Failed File-Validation, send email asking to resubmit
        return
    else:
        email_msg = "1) Analysis of the Zip File: Passed"
###################################################################################################################################
## validation step 1: ensure file names supplied are what was expected
    missing_list = []
    acc_participant_data, missing_list = load_data(s3_client, bucket, file_path, "Accrual_Participant_Info.csv", missing_list)
    acc_visit_data, missing_list = load_data(s3_client, bucket, file_path, "Accrual_Visit_Info.csv", missing_list)
    acc_vaccine_data, missing_list = load_data(s3_client, bucket, file_path, "Accrual_Vaccination_Status.csv", missing_list)
    
    all_files = s3_client.list_objects_v2(Bucket=bucket, Prefix=file_path.replace("File_Validation_Results/Result_Message.txt","UnZipped_Files"))["Contents"]
    submit_files = [i["Key"] for i in all_files]
    submit_files = [i.split("/")[-1] for i in submit_files]
    
    #templates are xlsx, but submitted files are csv, convertes formats to match
    template_files = [i.replace(".xlsx", ".csv") for i in template_files if len(i) > 0]
    
    extra_files = [i for i in submit_files if i not in template_files]
    
    if len(missing_list) == 0:
        email_msg = email_msg +  "\n2) Analysis of the file names within Zip File: Passed"
    
    if len(missing_list) > 0 or len(extra_files) > 0:
        missing_str = ',  '.join(missing_list)
        extra_str = ',  '.join(extra_files)
        
        email_msg = email_msg +  ("\n2) Analysis of the file names within Zip File: Failed\n "+ 
                                  f"\n      Files: {missing_str} were expected but are missing." +
                                  f"\n      Files: {extra_str} were found and not correct." +
                                  "\n\n Please correct file names and resubmit the accrual submission")
        send_error_email(ssm, sub_name, [], [], email_msg, slack_pass, slack_fail, bucket, file_path,
                         pass_bucket, fail_bucket, s3_client, s3_resource, site_name)     #retreive file and add to email as attachment
        return
######################################################################################################################################
## validation step 2: ensure that column names in files match templates provided (spelling and puncutaion matter)
    error_list = pd.DataFrame({"File_name": [], "Column_Name": [], 'Error_Message': []})
    erorr_list = check_cols(error_list, acc_participant_data, part_cols, "Accrual_Participant_Info")
    erorr_list = check_cols(error_list, acc_visit_data, visit_cols, "Accrual_Visit_Info")
    erorr_list = check_cols(error_list, acc_vaccine_data, vacc_cols, "Accrual_Vaccination_Status")

    if len(erorr_list) == 0:
        email_msg = email_msg +  "\n3) Analysis of the Column Names: Passed"

    if len(erorr_list) > 0:
        email_msg = email_msg + "\n3) Analysis of the Column Names: Failed"
        email_msg = email_msg + "\n      Attachment: Column_Errors_Found.csv has list of errors found"
        email_msg = email_msg + "\n\n Please correct file names and resubmit the accrual submission"
        
        error_key = file_path.replace("File_Validation_Results/Result_Message.txt", "Data_Errors/Column_Errors_Found.csv")
        make_attachment(ssm, sub_name, s3_client, erorr_list, bucket, error_key, email_msg,
                        "Column_Errors_Found.csv", slack_pass, slack_fail, file_path, pass_bucket, fail_bucket, s3_resource, site_name)
        return
######################################################################################################################################
## validation step 3: ensure that participants and visits align across the sheets
    acc_visit_data.replace("Baseline(1)", 1, inplace=True)
    acc_vaccine_data.replace("Baseline(1)", 1, inplace=True)

    acc_visit_data["Visit_Number"] = [int(i) for i in acc_visit_data["Visit_Number"]]
    acc_vaccine_data["Visit_Number"] = [int(i) for i in acc_vaccine_data["Visit_Number"]]

    check_part = acc_participant_data[["Research_Participant_ID", "Age"]].merge(acc_visit_data[["Research_Participant_ID"]], on="Research_Participant_ID", how="outer", indicator="Part_Visit")            
    error_data_1 = check_part.query("Part_Visit == 'left_only'")
    error_data_2 = check_part.query("Part_Visit == 'right_only'")
    part_errors_1 = pd.DataFrame({"Research_Participant_ID": error_data_1["Research_Participant_ID"], "Visit_Num": 'All',
                                  'Error_Message': "Participant has a Demographic Data, but missing coresponding visit in Accrual_Visit_Info.csv "})
                                  
    part_errors_2 = pd.DataFrame({"Research_Participant_ID": error_data_2["Research_Participant_ID"], "Visit_Num": 'All',
                                  'Error_Message': "Participant exists in Accrual_Visit_Info.csv but is missing from Accrual_Participant_Info.csv "})
                                  
    part_errors = pd.concat([part_errors_1, part_errors_2])

    check_visit = acc_visit_data.merge(acc_vaccine_data, on=["Research_Participant_ID","Visit_Number"], how="outer", indicator="Part_Vacc")
    visit_errors = check_visit.query("Part_Vacc in ['left_only']")   # flags data were participant not in visit or vise vera
    vacc_errors = check_visit.query("Part_Vacc in ['right_only']")   # flags data were participant not in visit or vise vera

    visit_errors = pd.DataFrame({"Research_Participant_ID": visit_errors["Research_Participant_ID"], "Visit_Num": visit_errors["Visit_Number"],
                                'Error_Message': "Participant has a Visit in Visit Data, but missing coresponding visit in Accrual_Vaccination_Status.csv"})
    vacc_errors = pd.DataFrame({"Research_Participant_ID": vacc_errors["Research_Participant_ID"], "Visit_Num": vacc_errors["Visit_Number"],
                                'Error_Message': "Participant has a visit in vaccination history, but missing coresponding visit in Visit_Info.csv"})

    all_error_data = pd.concat([part_errors, visit_errors, vacc_errors])
    all_error_data.drop_duplicates(inplace=True)
    
    if len(all_error_data) == 0:
        email_msg = email_msg +  "\n4) Analysis Cross Sheet Rules: Passed"
    
    if len(all_error_data) > 0:
        error_key = file_path.replace("File_Validation_Results/Result_Message.txt", "Data_Errors/Cross_Sheet_Errors_Found.csv")
        email_msg = email_msg + "\n4) Analysis Cross Sheet Rules: Failed"
        email_msg = email_msg + "\n      Attachment: Cross_Sheet_Errors_Found.csv has list of errors found"
        email_msg = email_msg + "\n\nPlease correct these discrepancies and resubmit the accrual submission"
        
        make_attachment(ssm, sub_name, s3_client, all_error_data, bucket, error_key, email_msg, 
                        "Cross_Sheet_Errors_Found.csv", slack_pass, slack_fail, file_path, pass_bucket, fail_bucket, s3_resource, site_name)
        return
######################################################################################################################################   
## validation step 5: Validate the Accrual Participant file
    all_error_data  = []
    try:
        part_errors = check_part_rules(acc_participant_data, cbc_name_list[curr_cbc][1])
    except Exception as e:
        email_msg = email_msg + "\n5) Analysis of Accrual_Participant_Info: Failed"
        email_msg = email_msg + f"\n         {e}"
        part_errors = -1
    all_error_data = get_error_data(all_error_data, part_errors)
       
    try:  
        visit_errors = check_visit_rules(acc_visit_data, cbc_name_list[curr_cbc][1])
    except Exception as e:
        email_msg = email_msg + "\n6) Analysis of Accrual_Visit_Info: Failed"
        email_msg = email_msg + f"\n         {e}"
        visit_errors = -1
    all_error_data = get_error_data(all_error_data, visit_errors)
 
    try:
        vaccine_errors = check_vaccine_rules(acc_vaccine_data, cbc_name_list[curr_cbc][1])
    except Exception as e:
        vaccine_errors = -1
        email_msg = email_msg + "\n7) Analysis of Accrual_Vaccination_Status: Failed"
        email_msg = email_msg + f"\n         {e}"
    all_error_data = get_error_data(all_error_data, vaccine_errors)
 
    if len(part_errors) > 0:
        email_msg = email_msg + "\n5) Analysis of Accrual_Participant_Info: Failed"
        email_msg = email_msg + f"\n           Accrual_Participant_Info was checked and has {len(part_errors)} found"
    elif len(part_errors) == 0:
        email_msg = email_msg + "\n5) Analysis of Accrual_Participant_Info: Passed"
        
    if len(visit_errors) > 0:
        email_msg = email_msg + "\n6) Analysis of Accrual_Visit_Info: Failed"
        email_msg = email_msg + f"\n           Accrual_Visit_Info was checked and has {len(visit_errors)} found"
    elif len(visit_errors) == 0:
        email_msg = email_msg + "\n6) Analysis of Accrual_Visit_Info: Passed"
        
    if len(vaccine_errors) > 0:
        email_msg = email_msg + "\n7) Analysis of Accrual_Vaccination_Status: Failed"
        email_msg = email_msg + f"\n           Accrual_Vaccination_Status was checked and has {len(vaccine_errors)} found"
    elif len(vaccine_errors) == 0:
        email_msg = email_msg + "\n7) Analysis of Accrual_Vaccination_Status: Passed"

    error_key = file_path.replace("File_Validation_Results/Result_Message.txt", "Data_Errors/Accrual_Error_Report.csv")
    make_attachment(ssm, sub_name, s3_client, all_error_data, bucket, error_key, email_msg,
                    "Accrual_Error_Report.csv", slack_pass, slack_fail, file_path, pass_bucket, fail_bucket, s3_resource, site_name, curr_cbc)

def get_error_data(error_df, file_df):
    if isinstance(file_df, pd.DataFrame):
        if len(error_df) == 0:
            error_df = file_df
        else:
            error_df = pd.concat([error_df, file_df])
    return error_df

def make_attachment(ssm, sub_name, s3_client, all_error_data, bucket, error_key, email_msg, file_name, slack_pass, slack_fail, file_path, pass_bucket, fail_bucket, s3_resource, site_name, curr_cbc):
    if len(all_error_data) > 0:
        wr.s3.to_csv(df=all_error_data,    path=f's3://{bucket}/{error_key}')       #write file to s3
        file_attach = s3_client.get_object(Bucket=bucket, Key=error_key)
        file_attach = file_attach.get('Body')
        file_attach = file_attach.read()
        
        attachment = MIMEApplication(file_attach)
        attachment.add_header('Content-Disposition', 'attachment', filename=file_name)
    else:
        attachment = []     # no errors found so no file to attach
    send_error_email(ssm, sub_name, attachment, all_error_data, email_msg, slack_pass, slack_fail, bucket,
                     file_path, pass_bucket, fail_bucket, s3_client, s3_resource, site_name, curr_cbc)     
###########################################################################################################################################
def load_data(s3_client, bucket, file_path, file_name, missing_list):
    part_key = file_path.replace("File_Validation_Results/Result_Message.txt", "UnZipped_Files/" + file_name)
    try:
        resp = s3_client.get_object(Bucket=bucket, Key=part_key)
        data_table = pd.read_csv(resp['Body'], na_filter=False)
    except Exception as e:
        data_table = []
        missing_list.append(file_name)
    finally:
        return data_table, missing_list

def check_cols(error_list, acc_df, temp_df, table):
    in_file_not_in_template = [i for i in acc_df if i not in temp_df]
    in_template_not_in_file = [i for i in temp_df if i not in acc_df]
    if len(in_file_not_in_template) > 0:
        for i in in_file_not_in_template:
            error_list.loc[len(error_list.index)] = [table, i, "Column found in File but not valid (not in template)"]
    if "Comments" in in_template_not_in_file:
        in_template_not_in_file.remove("Comments")   #this column is not mandatory and if missing do not fail this step
    
    if len(in_template_not_in_file) > 0:
        for i in in_template_not_in_file:
            error_list.loc[len(error_list.index)] = [table, i, "Column exists in the tempate, but missing from supplied file"]
    return error_list
    
def display_error_line(ex):
    trace = []
    tb = ex.__traceback__
    while tb is not None:
        trace.append({"filename": tb.tb_frame.f_code.co_filename,
                      "name": tb.tb_frame.f_code.co_name,
                      "lineno": tb.tb_lineno})
        tb = tb.tb_next
    print(str({'type': type(ex).__name__, 'message': str(ex), 'trace': trace}))
    return(str({'type': type(ex).__name__, 'message': str(ex), 'trace': trace}))
    
def make_csv(df):
    with io.StringIO() as buffer:
        df.to_csv(buffer)
        return buffer.getvalue()
        
def send_error_email(ssm, file_name, attachment, error_list, email_msg, slack_pass, slack_fail, bucket, file_path, pass_bucket, fail_bucket, s3_client, s3_resource, site_name, curr_cbc):
    http = urllib3.PoolManager()
    USERNAME_SMTP = ssm.get_parameter(Name="USERNAME_SMTP", WithDecryption=True).get("Parameter").get("Value")
    PASSWORD_SMTP = ssm.get_parameter(Name="PASSWORD_SMTP", WithDecryption=True).get("Parameter").get("Value")
    HOST = "email-smtp.us-east-1.amazonaws.com"
    PORT = 587
    
    try:
        #RECIPIENT_RAW = ssm.get_parameter(Name="Shipping_Manifest_Recipents", WithDecryption=True).get("Parameter").get("Value")
        #RECIPIENT = RECIPIENT_RAW.replace(" ", "")
        RECIPIENT_LIST = ["patrick.breads@nih.gov"]
        SUBJECT = f'Accrual Submission Feedback: {file_name}'
        SENDERNAME = 'SeroNet Data Team (Data Curation)'
        SENDER = ssm.get_parameter(Name="sender-email", WithDecryption=True).get("Parameter").get("Value")
        
        for recipient in RECIPIENT_LIST:
            msg_text = ""
            msg_text += "Your accrual submission has been analyzed by the validation software. \n"
            msg_text += email_msg 

            msg = MIMEMultipart('alternative')
            msg['Subject'] = SUBJECT
            msg['From'] = email.utils.formataddr((SENDERNAME, SENDER))
            if len(error_list) > 0:
                msg.attach(attachment)
                msg_text += f"\n\nAn Error file was created and attached to this email"
                msg_text += f"\nLet me know if you have any questions\n"
            msg['To'] = recipient
            part1 = MIMEText(msg_text, "plain")
            msg.attach(part1)
            
            send_email_func(HOST, PORT, USERNAME_SMTP, PASSWORD_SMTP, SENDER, recipient, msg)
            print("email has been sent")
            
            if len(error_list) > 0:
                move_submission(file_name, error_list, bucket, fail_bucket, file_path, s3_client, s3_resource, site_name, curr_cbc)
            elif len(error_list) == 0:
                move_submission(file_name, error_list, bucket, pass_bucket, file_path, s3_client, s3_resource, site_name, curr_cbc)
            else: 
                print("unable to move submission due to negative error value")
                       
            data={"text": email_msg}
            r=http.request("POST", slack_pass, body=json.dumps(data), headers={"Content-Type":"application/json"})
            print("submission has been moved")
    except Exception as e:
        print(e)
        #data={"text": display_error_line(e)}
        #r=http.request("POST", slack_fail, body=json.dumps(data), headers={"Content-Type":"application/json"})

def send_email_func(HOST, PORT, USERNAME_SMTP, PASSWORD_SMTP, SENDER, recipient, msg):
    server = smtplib.SMTP(HOST, PORT)
    server.ehlo()
    server.starttls()
    #stmplib docs recommend calling ehlo() before & after starttls()
    server.ehlo()
    server.login(USERNAME_SMTP, PASSWORD_SMTP)

    server.sendmail(SENDER, recipient, msg.as_string())
    server.close()

def move_submission(file_name, error_list, curr_bucket, new_bucket, file_path, s3_client, s3_resource, site_name, curr_cbc):
    # curr_bucket = seronet-demo-cbc-destination
    # file_path = "Accrual_Need_To_Validate/cbc02/2023-05-09-10-27-51/submission_007_accrual_submission_5_9_23.zip/"
    # make sure the submission csv is the last one to be moved
    all_files = s3_client.list_objects_v2(Bucket=curr_bucket, Prefix=file_path[:30])["Contents"]
    sub_files = [i["Key"] for i in all_files if "UnZipped_Files/submission.csv" not in i["Key"]]
    submission_csv_key = [i["Key"] for i in all_files if "UnZipped_Files/submission.csv" in i["Key"]]
    sub_files.append(submission_csv_key[0])
    cbc_key = curr_cbc + "/"
    print(sub_files)

    for curr_key in sub_files:  
        new_key = curr_key.replace("Accrual_Need_To_Validate", f"Monthly_Accrual_Reports/{site_name}")
        new_key = new_key.replace(cbc_key, "")
        source = {'Bucket': curr_bucket, 'Key': curr_key}               # files to copy
        try:
            s3_resource.meta.client.copy(source, new_bucket, new_key)
            print(f"atempting to delete {curr_bucket}/{curr_key}")
            s3_client.delete_object(Bucket=curr_bucket, Key=curr_key)
            
        except Exception as error:
            print('Error Message: {}'.format(error))
        
def convert_data_type(v):
    if isinstance(v, (datetime.datetime, datetime.time, datetime.date)):
        return v
    if str(v).find('_') > 0:
        return v
    try:
        float(v)
        if (float(v) * 10) % 10 == 0:
            return int(float(v))
        return float(v)
    except ValueError:
        try:
            v = parse(v)
            return v
        except ValueError:
            return v
        except TypeError:
            return str(v)

def add_df_cols(df_name, field_name, error_msg):
    df_name["Error_Message"] = "None"
    df_name["Column_Name"] = field_name
    if len(df_name) > 0:
        df_name["Error_Message"] = error_msg
    df_name = df_name[["Column_Name", field_name, "Error_Message"]]
    return df_name
    
def check_id_field(data_table, re, field_name, pattern_str, CBC_ID, pattern_error):
    wrong_cbc_id = data_table[data_table[field_name].apply(lambda x: x[:2] not in [str(CBC_ID)])]
    invalid_id = data_table[data_table[field_name].apply(lambda x: re.compile('^' + str(CBC_ID) + pattern_str).match(str(x)) is None)]

    wrong_cbc_id = add_df_cols(wrong_cbc_id, field_name, "CBC code found is wrong. Expecting CBC Code (" + str(CBC_ID) + ")")
    invalid_id = add_df_cols(invalid_id, field_name,  "ID is Not Valid Format, Expecting " + pattern_error)

    error_table = pd.concat([wrong_cbc_id, invalid_id])
    error_table = error_table.rename(columns={field_name: "Column_Value"})
    return error_table

def check_is_number(data_table, curr_col, min_val, max_val, **kwargs):
    try:
        Not_a_number = data_table[data_table[curr_col].apply(lambda x: isinstance(x, (int, float)) is False)]
        numeric_data = data_table[data_table[curr_col].apply(lambda x: isinstance(x, (int, float)) is True)]
        out_of_range = numeric_data.query("`{0}` > @max_val or `{0}` < @min_val".format(curr_col))
    except Exception as e:
        print(e)

    if "NA_Allowed" in kwargs:
        if kwargs["NA_Allowed"] is True:
            Not_a_number = Not_a_number.query(f"`{curr_col}` != 'N/A'")
            Not_a_number = Not_a_number.query(f"`{curr_col}` != 'Not Reported'")

    Not_a_number = add_df_cols(Not_a_number, curr_col, "Value is not a numeric value")
    out_of_range = add_df_cols(out_of_range, curr_col, f"Value must be a number between {min_val} and {max_val}")

    error_table = pd.concat([Not_a_number, out_of_range])
    error_table = error_table.rename(columns={curr_col: "Column_Value"})
    return error_table

def check_if_list(data_table, curr_col, list_values):
    x = [i for i in data_table.index if data_table[curr_col][i] not in list_values]
    error_data = data_table.loc[x]
    error_data = add_df_cols(error_data, curr_col, f"Value is not an acceptable term, should be one the following: {list_values}")
    error_data = error_data.rename(columns={curr_col: "Column_Value"})
    return error_data

def check_if_date(data_table, curr_col):
    error_table = pd.DataFrame(columns=["Column_Name", "Column_Value", "Error_Message"])
    for curr_index in data_table.index:
        try:
            curr_date = data_table[curr_col][curr_index]
            try:
                future_logic = curr_date.date() > datetime.date.today()  #date in the future
            except Exception:
                 future_logic = curr_date > datetime.date.today()  #date in the future
            weekday_logic = curr_date.strftime('%A') != "Sunday"  #date not a sunday
            if future_logic is True and weekday_logic is False:
                error_msg = "Value is a Sunday but exists in the future"
            if future_logic is False and weekday_logic is True:
                error_msg = "Value is a valid date but is not a Sunday"
            if future_logic is True and weekday_logic is True:
                error_msg = "Value is a future date and is also not a Sunday"
            else:
                continue
            error_table.loc[len(error_table)] = [curr_col, data_table[curr_col][curr_index], error_msg]
        except Exception:
            error_table.loc[len(error_table)] = [curr_col, data_table[curr_col][curr_index], "Value is not a parsable date"]
    return error_table

def check_part_rules(participant_data, cbc_id):
    error_table = pd.DataFrame(columns=["Column_Name", "Column_Value", "Error_Message"])
    for curr_col in participant_data.columns:
        participant_data[curr_col] = [convert_data_type(c) for c in participant_data[curr_col]]
        if curr_col == "Research_Participant_ID":
            pattern_str = '[_]{1}[A-Z, 0-9]{6}$'
            error_table = pd.concat([error_table, check_id_field(participant_data, re, curr_col, pattern_str, cbc_id, "XX_XXXXXX")])
        if curr_col == "Age":
            error_table = pd.concat([error_table,check_is_number(participant_data, curr_col, 1, 90, NA_Allowed=True)])
        if (curr_col in ['Race', 'Ethnicity', 'Gender', 'Sex_At_Birth']):
            if (curr_col in ['Race']):
                list_values = ['White', 'American Indian or Alaska Native', 'Black or African American', 'Asian',
                               'Native Hawaiian or Other Pacific Islander', 'Other', 'Multirace', 'Unknown']  # removing 'Not Reported'
            elif (curr_col in ['Ethnicity']):
                list_values = ['Hispanic or Latino', 'Not Hispanic or Latino', 'Unknown',  'Not Reported']
            elif (curr_col in ['Gender', 'Sex_At_Birth']):
                list_values = ['Male', 'Female', 'InterSex', 'Not Reported', 'Prefer Not to Answer', 'Unknown', 'Other']
            error_table = pd.concat([error_table, check_if_list(participant_data, curr_col, list_values)])
        if curr_col in "Week_Of_Visit_1":
            error_table = pd.concat([check_if_date(participant_data, curr_col)])
    return error_table
    
def check_visit_rules(visit_data, cbc_id):
    error_table = pd.DataFrame(columns=["Column_Name", "Column_Value", "Error_Message"])
    for curr_col in visit_data.columns:
        visit_data[curr_col] = [convert_data_type(c) for c in visit_data[curr_col]]
        if curr_col == "Research_Participant_ID":
            pattern_str = '[_]{1}[A-Z, 0-9]{6}$'
            error_table = pd.concat([error_table, check_id_field(visit_data, re, curr_col, pattern_str, cbc_id, "XX_XXXXXX")])
        if (curr_col in ['Primary_Cohort', 'SARS_CoV_2_Infection_Status', 'Unscheduled_Visit', 'Unscheduled_Visit_Purpose',
                         'Lost_To_FollowUp', 'Final_Visit', 'Collected_In_This_Reporting_Period', 'Visit_Number', 'Serum_Shipped_To_FNL', 'PBMC_Shipped_To_FNL']):
            if curr_col in ['Primary_Cohort']:
                list_values = ['Autoimmune', 'Cancer', 'Healthy Control','HIV', 'IBD', 'Pediatric', 'Transplant', 'PRIORITY', 'Chronic Conditions', 'Convalescent']
                visit_data[curr_col] = [i.split("|")[0] for i in visit_data[curr_col]]
            if curr_col in ['SARS_CoV_2_Infection_Status']:
                list_values = ['Has Reported Infection', 'Has Not Reported Infection', 'Not Reported']
            if curr_col in ['Unscheduled_Visit', 'Final_Visit', 'Lost_To_FollowUp']:
                list_values = ['Yes', 'No', 'Unknown']
            if curr_col in ['Collected_In_This_Reporting_Period']:
                list_values = ['Yes', 'No']
            if curr_col in ['Unscheduled_Visit_Purpose']:
                list_values = ['Breakthrough COVID', 'Completion of Primary Vaccination Series', 'Completion of Booster', 'Other', 'N/A']
            if curr_col in ['Serum_Shipped_To_FNL', 'PBMC_Shipped_To_FNL']:
                list_values = ['Yes', 'No', 'N/A']
            if (curr_col in ['Visit_Number']):
                list_values = ["Baseline(1)"] + [str(i) for i in list(range(1,30))] + [i for i in list(range(1,30))]
            error_table = pd.concat([error_table, check_if_list(visit_data, curr_col, list_values)])
        if (curr_col in ['Visit_Date_Duration_From_Visit_1']):
             error_table = pd.concat([error_table,check_is_number(visit_data, curr_col, -1000, 1000, NA_Allowed=False)])
        if (curr_col in ['Serum_Volume_For_FNL', 'PBMC_Concentration', 'Num_PBMC_Vials_For_FNL']):
            error_table = pd.concat([error_table,check_is_number(visit_data, curr_col, -1, 1e9, NA_Allowed=True)])
    return error_table

def check_vaccine_rules(vaccine_data, cbc_id):
    error_table = pd.DataFrame(columns=["Column_Name", "Column_Value", "Error_Message"])
    for curr_col in vaccine_data.columns:
        vaccine_data[curr_col] = [convert_data_type(c) for c in vaccine_data[curr_col]]
        if curr_col == "Research_Participant_ID":
            pattern_str = '[_]{1}[A-Z, 0-9]{6}$'
            error_table = pd.concat([error_table, check_id_field(vaccine_data, re, curr_col, pattern_str, cbc_id, "XX_XXXXXX")])

        if (curr_col in ['Visit_Number', 'Vaccination_Status', 'SARS-CoV-2_Vaccine_Type']):
            if curr_col in ['Vaccination_Status']:
                list_values = (['Unvaccinated', 'No vaccination event reported', 'Dose 1 of 1', 'Dose 1 of 2', 'Dose 2 of 2', 'Dose 2', 'Dose 3', 'Dose 4'] +
                              ["Booster " + str(i) for i in list(range(1,10))] + 
                              ["Booster " + str(i) + ":Bivalent" for i in list(range(1,10))] + 
                              ["Dose " + str(i) + ":Bivalent" for i in list(range(3,10))])
            if curr_col in ['SARS-CoV-2_Vaccine_Type']:
                list_values = ['Johnson & Johnson', 'Moderna', 'Pfizer', 'Unknown', 'N/A', 'Janssen', 'Sputnik V']
            if (curr_col in ['Visit_Number']):
                list_values = ["Baseline(1)"] + [str(i) for i in list(range(1,30))] + [i for i in list(range(1,30))]
            error_table = pd.concat([error_table, check_if_list(vaccine_data, curr_col, list_values)])
        if (curr_col in ['SARS-CoV-2_Vaccination_Date_Duration_From_Visit1']):
            error_table = pd.concat([error_table,check_is_number(vaccine_data, curr_col, -1e9, 1e9, NA_Allowed=True)])
    return error_table
