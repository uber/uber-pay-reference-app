module.exports = {
    badRequest: function(res, errCode, value) {
        if(value) {
            console.log(" error : " + errCode + " " + value.error);
            res.statusText = JSON.stringify(value);
            return res.status(errCode).send();
        }
        console.log(" error : " + errCode.error);
        res.statusText = JSON.stringify(errCode);
        return res.status(403).send();
    }
};