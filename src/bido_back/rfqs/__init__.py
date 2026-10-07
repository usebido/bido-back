"""The RFQ flow: RFQs from text, suppliers and contacts, invites, and sending.

service.py    create / read / edit RFQs, invites, prepare_send, approval queue
suppliers.py  suppliers and their contacts (personal data only in contacts)
send.py       the `send_rfq_email` act tool (run only by the approval executor)
mailbox.py    the seam to the buyer's connected mailbox sender
agents.py     the intake and email-writer agents, behind a replaceable seam
"""
